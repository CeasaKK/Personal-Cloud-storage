# Deploying on the mini PC

This guide covers the hardware you chose: **six disks in a 4+2 Reed–Solomon profile**.

## 1. Hardware

| Part | Notes |
|---|---|
| Mini PC | x86-64 with AVX2 (any recent Intel N100/N305 or Ryzen). The erasure code's SIMD kernel is compiled with `-march=native` on this machine. |
| Drive bays | **6 bays**, e.g. one 6-bay enclosure, or two enclosures. A 4-bay box can't hold 4+2. The enclosure should support UAS and SAT passthrough so SMART health data reaches the dashboard. |
| Disks | 6 × 4 TB NAS-rated drives (WD Red Plus / Seagate IronWolf) give **16 TB usable** and survive **any two** disk failures. |
| System SSD | Holds the OS, SQLite metadata, thumbnails and upload staging. A 256 GB SSD is plenty. The metadata is also snapshotted daily onto the erasure-coded disks. |

You can start with 2 disks. The store runs in **mirror mode**, which is the same code path
with k=1. When you reach 6 disks, run `cloudstore restripe` to convert existing data to 4+2
in the background.

## 2. Operating system and install

Ubuntu Server 24.04 LTS (or Debian 12), then:

```bash
git clone https://github.com/CeasaKK/Personal-Cloud-storage.git
cd Personal-Cloud-storage
sudo ./deploy/install.sh
```

## 3. Prepare the disks

Give each disk one XFS filesystem (ext4 works too) and mount it **by UUID** with `nofail`,
so one dead disk never blocks booting:

```bash
sudo mkfs.xfs -f -L cs-d1 /dev/sdX        # repeat for each disk: cs-d1 … cs-d6
sudo mkdir -p /srv/cloud/disks/d{1..6}
sudo blkid | grep cs-d                     # note each UUID
```

`/etc/fstab`, one line per disk:

```
UUID=<uuid-of-cs-d1>  /srv/cloud/disks/d1  xfs  defaults,noatime,nofail,x-systemd.device-timeout=10s  0 2
```

```bash
sudo mount -a
sudo chown cloudstore: /srv/cloud/disks/d*
for i in 1 2 3 4 5 6; do
  sudo -u cloudstore env $(cat /etc/cloudstore.env | xargs) /opt/cloudstore/venv/bin/cloudstore disk add /srv/cloud/disks/d$i --label disk-$i
done
```

Each disk gets a `disk.json` identity file, so the system recognises a disk even if its
mount path changes later.

## 4. Password and start

```bash
sudo -u cloudstore env $(cat /etc/cloudstore.env | xargs) /opt/cloudstore/venv/bin/cloudstore set-password
sudo systemctl start cloudstore
journalctl -u cloudstore -f
```

## 5. Remote access with Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale serve --bg 8000
```

`tailscale serve` publishes `https://<machine>.<tailnet>.ts.net` to your tailnet only, with
an automatic HTTPS certificate. Nothing is exposed to the public internet and no router
port forwarding is needed. Install Tailscale on your iPhone and laptop and sign in to the
same tailnet.

* Web app: `https://<machine>.<tailnet>.ts.net`
* iOS app: sign in with `<machine>.<tailnet>.ts.net` (see `ios/README.md`)

## 6. Operations

| Task | Command |
|---|---|
| Disk and profile overview | `cloudstore disk list`, `cloudstore status`, or the web **Storage** page |
| Force a full integrity check | `cloudstore scrub --full`, or **Scrub now** on the Storage page |
| Replace a failed disk | Mount the new disk, then run `cloudstore disk replace <old-id> /srv/cloud/disks/d7`. The server rebuilds it in the background and shows progress on the Storage page. |
| Grew from mirror to 6 disks | `cloudstore restripe` |
| Metadata snapshot now | `cloudstore snapshot` (also runs daily automatically) |
| System SSD died | Reinstall, re-add the disks (`disk add` for each), then `cloudstore recover-index --restore-metadata --reingest` and `cloudstore scrub --full` |

(Run CLI commands as `sudo -u cloudstore env $(cat /etc/cloudstore.env | xargs) /opt/cloudstore/venv/bin/cloudstore …`.)

### What happens when a disk fails

1. The health monitor (every 60 s) or the first read error marks the disk **failed**.
   The Storage page turns amber: "1 disk needs attention — 1 more failure tolerated".
2. Reads keep working. Each read rebuilds the missing piece from parity, at about 18%
   extra latency (docs/benchmarks.md §4). New uploads still commit, with the shard for
   the dead disk recorded as missing.
3. Swap in a new disk and run `disk replace`. The rebuild runs throttled (150 MB/s by
   default) and is disk-bound at about 6–7 h for a full 4 TB disk.

### Bit rot

Every shard carries a BLAKE2b checksum. The background scrub re-reads everything at
least every 30 days, rate-limited so it doesn't disturb normal use, and rewrites any
corrupted shard from the surviving ones. Scrub history is on the Storage page.
