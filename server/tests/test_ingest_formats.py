import io
import struct

import pytest
from PIL import Image

from cloudstore.ingest import mp4
from cloudstore.ingest.classify import classify
from cloudstore.ingest.metadata import image_metadata, video_metadata


def box(t: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + t + payload


def fullbox(t: bytes, payload: bytes, version: int = 0) -> bytes:
    return box(t, bytes([version, 0, 0, 0]) + payload)


def iphone_like_mov() -> bytes:
    """ftyp qt + mdat + moov{mvhd, trak{tkhd, mdia{hdlr vide}}, meta{keys, ilst}} (moov at the end,
    like many camera files)."""
    ctime = 3_800_000_000  # seconds since 1904
    mvhd = fullbox(b"mvhd", struct.pack(">IIII", ctime, ctime, 600, 600 * 12) + bytes(80))
    tkhd = fullbox(b"tkhd", bytes(76) + struct.pack(">II", 1920 << 16, 1080 << 16))
    hdlr = fullbox(b"hdlr", bytes(4) + b"vide" + bytes(12) + b"Core Media Video\0")
    trak = box(b"trak", tkhd + box(b"mdia", hdlr))
    keys_list = [b"com.apple.quicktime.location.ISO6709", b"com.apple.quicktime.creationdate",
                 b"com.apple.quicktime.model"]
    keys = fullbox(b"keys", struct.pack(">I", len(keys_list)) +
                   b"".join(struct.pack(">I", 8 + len(k)) + b"mdta" + k for k in keys_list))
    values = [b"+28.6139+077.2090+215.000/", b"2025-03-14T18:05:09+0530", b"iPhone 15 Pro"]
    ilst = box(b"ilst", b"".join(
        box(struct.pack(">I", i + 1), box(b"data", struct.pack(">II", 1, 0) + v)) for i, v in enumerate(values)))
    meta = box(b"meta", box(b"hdlr", bytes(8) + b"mdta" + bytes(12)) + keys + ilst)
    moov = box(b"moov", mvhd + trak + meta)
    ftyp = box(b"ftyp", b"qt  " + struct.pack(">I", 0) + b"qt  ")
    mdat = box(b"mdat", bytes(50_000))
    return ftyp + mdat + moov


def test_mov_classified_and_parsed(tmp_path):
    data = iphone_like_mov()
    assert classify(data[:64], "IMG_0001.MOV").mime == "video/quicktime"
    meta = mp4.parse(io.BytesIO(data))
    assert (meta["width"], meta["height"]) == (1920, 1080)
    assert meta["duration"] == 12.0
    assert abs(meta["lat"] - 28.6139) < 1e-6 and abs(meta["lon"] - 77.2090) < 1e-6
    assert meta["model"] == "iPhone 15 Pro"
    # the Apple creationdate (with its +05:30 offset) wins over mvhd's UTC time
    assert meta["creation_ts"] == 1741955709.0
    p = tmp_path / "v.mov"
    p.write_bytes(data)
    vm = video_metadata(p)
    assert vm["camera_model"] == "iPhone 15 Pro" and vm["exif_has_tz"]


def test_truncated_video_does_not_crash(tmp_path):
    p = tmp_path / "broken.mp4"
    p.write_bytes(iphone_like_mov()[:5000])
    assert video_metadata(p) == {}


def test_heic_roundtrip(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    img = Image.new("RGB", (640, 480), (40, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15 Pro"
    exif.get_ifd(0x8769)[0x9003] = "2025:01:02 03:04:05"
    exif[0x0112] = 6  # rotated 90°: width/height must swap
    p = tmp_path / "IMG_0002.HEIC"
    try:
        img.save(p, format="HEIF", exif=exif.tobytes())
    except Exception as e:  # encoder not bundled on this platform
        pytest.skip(f"HEIF encoder unavailable: {e}")
    head = p.read_bytes()[:64]
    assert classify(head, p.name).mime in ("image/heic", "image/heif")
    meta = image_metadata(Image.open(p))
    assert meta["camera_model"] == "iPhone 15 Pro"
    assert (meta["width"], meta["height"]) == (480, 640)


@pytest.mark.parametrize("name,head,tier", [
    ("a.jpg", b"\xff\xd8\xff\xe0" + bytes(60), "media"),
    ("a.png", b"\x89PNG\r\n\x1a\n" + bytes(56), "media"),
    ("a.dng", b"II*\x00" + bytes(60), "nonmedia"),
    ("a.CR3", bytes(4) + b"ftypcrx " + bytes(52), "nonmedia"),
    ("notes.txt", b"hello world" + bytes(53), "nonmedia"),
    ("a.mp4", b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(40), "media"),
])
def test_classification(name, head, tier):
    assert classify(head, name).tier == tier
