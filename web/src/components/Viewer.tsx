// Full-screen viewer: medium proxy for photos (original on demand), streamed original
// for videos (HTTP Range), keyboard navigation and an EXIF info panel.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, FileDetail, FileSummary, originalUrl, proxyUrl } from "../api";
import { bytes, dateTime } from "../format";
import { Icon } from "./Icon";

interface Props {
  items: FileSummary[];
  index: number;
  onIndex: (i: number) => void;
  onClose: () => void;
  onChanged: (f: FileSummary, removed?: boolean) => void;
}

export function Viewer({ items, index, onIndex, onClose, onChanged }: Props) {
  const f = items[index];
  const [detail, setDetail] = useState<FileDetail | null>(null);
  const [info, setInfo] = useState(() => {
    try {
      return localStorage.getItem("viewer.info") === "1";
    } catch {
      return false;
    }
  });
  const [full, setFull] = useState(false);

  useEffect(() => {
    setDetail(null);
    setFull(false);
    if (f) api.file(f.id).then(setDetail).catch(() => {});
  }, [f?.id]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowRight" && index < items.length - 1) onIndex(index + 1);
      if (e.key === "ArrowLeft" && index > 0) onIndex(index - 1);
      if (e.key === "i") toggleInfo();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // preload neighbours so arrow navigation is instant
  useEffect(() => {
    for (const j of [index - 1, index + 1]) {
      const n = items[j];
      if (n && n.kind === "image") new Image().src = proxyUrl(n.id);
    }
  }, [index, items]);

  if (!f) return null;

  function toggleInfo() {
    setInfo((v) => {
      try {
        localStorage.setItem("viewer.info", v ? "0" : "1");
      } catch {
        /* storage unavailable */
      }
      return !v;
    });
  }

  async function toggleFavorite() {
    const d = await api.setFavorite(f.id, !f.favorite);
    setDetail(d);
    onChanged({ ...f, favorite: d.favorite });
  }

  async function remove() {
    await api.trash([f.id]);
    onChanged(f, true);
  }

  const exif = detail?.exif ?? {};
  const exposure = [
    exif.FNumber ? `ƒ/${exif.FNumber}` : null,
    exif.ExposureTime ? `${Number(exif.ExposureTime) < 1 ? `1/${Math.round(1 / Number(exif.ExposureTime))}` : exif.ExposureTime}s` : null,
    exif.FocalLength ? `${exif.FocalLength}mm` : null,
    exif.ISOSpeedRatings || exif.PhotographicSensitivity ? `ISO ${exif.ISOSpeedRatings ?? exif.PhotographicSensitivity}` : null,
  ].filter(Boolean);

  return (
    <div className="viewer" role="dialog" aria-modal="true" aria-label={f.filename}>
      <div className="viewer-bar">
        <button className="icon-btn light" onClick={onClose} aria-label="Close">
          <Icon name="back" />
        </button>
        <div className="spacer" />
        <button className="icon-btn light" onClick={toggleInfo} aria-label="Info" aria-pressed={info}>
          <Icon name="info" />
        </button>
        <button className="icon-btn light" onClick={toggleFavorite} aria-label="Favourite">
          <Icon name={f.favorite ? "starFilled" : "star"} />
        </button>
        <a className="icon-btn light" href={originalUrl(f.id, true)} aria-label="Download original">
          <Icon name="download" />
        </a>
        <button className="icon-btn light" onClick={remove} aria-label="Move to trash">
          <Icon name="trash" />
        </button>
      </div>
      <div className="viewer-body">
        <div className="stage" onClick={(e) => e.target === e.currentTarget && onClose()}>
          {index > 0 && (
            <button className="nav prev" onClick={() => onIndex(index - 1)} aria-label="Previous">
              <Icon name="left" size={36} />
            </button>
          )}
          {f.kind === "video" ? (
            <video key={f.id} src={originalUrl(f.id)} poster={proxyUrl(f.id)} controls autoPlay playsInline />
          ) : (
            <img
              key={`${f.id}-${full}`}
              src={full ? originalUrl(f.id) : proxyUrl(f.id)}
              alt={f.filename}
              onDoubleClick={() => setFull(true)}
              title={full ? "Original" : "Double-click to load the original"}
            />
          )}
          {index < items.length - 1 && (
            <button className="nav next" onClick={() => onIndex(index + 1)} aria-label="Next">
              <Icon name="right" size={36} />
            </button>
          )}
        </div>
        {info && (
          <aside className="info-panel">
            <h2>Info</h2>
            <dl>
              <dt>Date</dt>
              <dd>{dateTime(f.capture_ts)}</dd>
              <dt>File</dt>
              <dd>
                {f.filename}
                <br />
                <span className="muted">
                  {f.width && f.height ? `${f.width} × ${f.height} · ` : ""}
                  {bytes(f.size)} · {f.mime}
                </span>
              </dd>
              {detail?.camera_model && (
                <>
                  <dt>Camera</dt>
                  <dd>
                    {detail.camera_make} {detail.camera_model}
                    {exposure.length > 0 && <div className="muted">{exposure.join("  ·  ")}</div>}
                    {typeof exif.LensModel === "string" && <div className="muted">{exif.LensModel}</div>}
                  </dd>
                </>
              )}
              {detail?.lat != null && detail?.lon != null && (
                <>
                  <dt>Location</dt>
                  <dd>
                    <Link to={`/map?lat=${detail.lat}&lon=${detail.lon}&z=15`} onClick={onClose}>
                      {detail.lat.toFixed(5)}, {detail.lon.toFixed(5)}
                    </Link>
                  </dd>
                </>
              )}
              {detail?.albums && detail.albums.length > 0 && (
                <>
                  <dt>Albums</dt>
                  <dd>{detail.albums.map((a) => a.name).join(", ")}</dd>
                </>
              )}
              <dt>Backed up</dt>
              <dd>
                {dateTime(detail?.upload_ts)}
                <div className="muted mono">sha256 {f.sha256.slice(0, 16)}…</div>
              </dd>
            </dl>
          </aside>
        )}
      </div>
    </div>
  );
}
