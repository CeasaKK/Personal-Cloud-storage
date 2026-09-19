// Storage dashboard: capacity, erasure profile, per-disk health, scrub history,
// rebuild progress, tier statistics, devices.

import { useEffect, useState } from "react";
import { api, StorageOverview } from "../api";
import { Icon } from "../components/Icon";
import { ago, bytes, dateTime } from "../format";

const STATUS_LABEL: Record<string, string> = {
  online: "Healthy",
  degraded: "Degraded",
  failed: "Failed",
  rebuilding: "Rebuilding",
  retired: "Retired",
};

export function StoragePage() {
  const [o, setO] = useState<StorageOverview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [scrubbing, setScrubbing] = useState(false);

  const load = () =>
    api
      .overview()
      .then((x) => {
        setO(x);
        setErr(null);
      })
      .catch((e) => setErr(String(e.message ?? e)));

  useEffect(() => {
    load();
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div className="page"><div className="banner error">{err}</div></div>;
  if (!o) return <div className="page"><div className="spinner" /></div>;

  const cap = o.capacity;
  const { k, m, mode } = cap.profile;
  const usedFrac = cap.raw_bytes ? cap.physical_stored_bytes / cap.raw_bytes : 0;
  const active = o.disks.filter((d) => d.status !== "retired");
  const bad = active.filter((d) => d.status === "failed" || d.status === "degraded");
  const margin = m - active.filter((d) => d.status === "failed").length;
  const nm = o.tiers.nonmedia_store;
  const lastRun = o.scrub.runs[0];

  return (
    <div className="page storage">
      <div className="page-title">Storage</div>

      <div className={`health-banner ${bad.length === 0 ? "ok" : margin > 0 ? "warn" : "crit"}`}>
        <Icon name="shield" />
        <div>
          <strong>
            {bad.length === 0
              ? `All ${active.length} disks healthy`
              : `${bad.length} disk${bad.length > 1 ? "s need" : " needs"} attention`}
          </strong>
          <div>
            {mode === "erasure"
              ? `Reed–Solomon ${k}+${m}: data survives any ${m} simultaneous disk failures`
              : `Mirror mode (${m + 1} copies) until ${k + m} disks are installed`}
            {bad.length > 0 && ` — ${Math.max(margin, 0)} more failure(s) tolerated right now`}
          </div>
        </div>
      </div>

      <div className="cards">
        <div className="card">
          <h3>Capacity</h3>
          <div className="big">{bytes(cap.logical_stored_bytes)}</div>
          <div className="muted">of {bytes(cap.usable_bytes)} usable</div>
          <div className="meter" role="meter" aria-valuenow={Math.round(usedFrac * 100)} aria-valuemin={0} aria-valuemax={100}>
            <span style={{ width: `${Math.min(100, usedFrac * 100)}%` }} />
          </div>
          <dl className="kv">
            <dt>Raw disk space</dt>
            <dd>{bytes(cap.raw_bytes)}</dd>
            <dt>On disk incl. parity</dt>
            <dd>{bytes(cap.physical_stored_bytes)}</dd>
            <dt>Efficiency</dt>
            <dd>
              {Math.round((k / (k + m)) * 100)}% <span className="muted">(mirroring: 50%)</span>
            </dd>
          </dl>
        </div>
        <div className="card">
          <h3>Library</h3>
          <dl className="kv">
            <dt>Photos & videos</dt>
            <dd>
              {o.tiers.media.n.toLocaleString()} · {bytes(o.tiers.media.bytes)}
            </dd>
            <dt>Files (non-media)</dt>
            <dd>
              {o.tiers.nonmedia.n.toLocaleString()} · {bytes(o.tiers.nonmedia.bytes)}
            </dd>
            <dt>Chunk dedup</dt>
            <dd>{nm.dedup_ratio ? `${nm.dedup_ratio.toFixed(2)}×` : "—"}</dd>
            <dt>Batch compression</dt>
            <dd>{nm.compression_ratio ? `${nm.compression_ratio.toFixed(2)}×` : "—"}</dd>
            <dt>Waiting to batch</dt>
            <dd>{bytes(nm.open_batch_bytes)}</dd>
            <dt>Ingest queue</dt>
            <dd>
              {(o.ingest.queued ?? 0) + (o.ingest.running ?? 0)} pending
              {o.ingest.failed ? <span className="err"> · {o.ingest.failed} failed</span> : null}
            </dd>
          </dl>
        </div>
        <div className="card">
          <h3>Integrity</h3>
          <dl className="kv">
            <dt>Stripes</dt>
            <dd>{(o.stripes.committed ?? 0).toLocaleString()}</dd>
            <dt>Degraded stripes</dt>
            <dd className={o.stripes.with_bad_shards ? "err" : ""}>{o.stripes.with_bad_shards ?? 0}</dd>
            <dt>Unrecoverable</dt>
            <dd className={o.stripes.unrecoverable ? "err" : ""}>{o.stripes.unrecoverable ?? 0}</dd>
            <dt>Oldest verification</dt>
            <dd>{o.scrub.oldest_verified_at ? ago(o.scrub.oldest_verified_at) : "not yet scrubbed"}</dd>
            <dt>Last scrub</dt>
            <dd>{lastRun ? `${ago(lastRun.started_at)} · ${lastRun.status}` : "never"}</dd>
            <dt>GF(2⁸) kernel</dt>
            <dd className="mono">{o.gf_backend}</dd>
          </dl>
          <button
            className="btn"
            disabled={scrubbing}
            onClick={async () => {
              setScrubbing(true);
              try {
                await api.scrubNow();
                await load();
              } finally {
                setScrubbing(false);
              }
            }}
          >
            {scrubbing ? "Scrubbing…" : "Scrub now"}
          </button>
        </div>
      </div>

      <h2>Disks</h2>
      <div className="disk-grid">
        {o.disks.map((d) => {
          const used = d.capacity_bytes && d.free_bytes != null ? 1 - d.free_bytes / d.capacity_bytes : 0;
          return (
            <div key={d.id} className={`disk ${d.status}`}>
              <div className="disk-head">
                <Icon name="disk" />
                <strong>{d.label}</strong>
                <span className={`pill ${d.status}`}>{STATUS_LABEL[d.status] ?? d.status}</span>
              </div>
              <div className="meter small">
                <span style={{ width: `${used * 100}%` }} />
              </div>
              <div className="muted small">
                {bytes(d.capacity_bytes && d.free_bytes != null ? d.capacity_bytes - d.free_bytes : null)} used of{" "}
                {bytes(d.capacity_bytes)} · {d.shards.toLocaleString()} shards
              </div>
              <dl className="kv small">
                <dt>I/O errors</dt>
                <dd className={d.error_count ? "err" : ""}>{d.error_count}</dd>
                {d.smart && (
                  <>
                    <dt>SMART</dt>
                    <dd className={d.smart.passed === false ? "err" : ""}>
                      {d.smart.passed === false ? "FAILING" : "passed"}
                      {d.smart.temperature ? ` · ${d.smart.temperature}°C` : ""}
                      {d.smart.power_on_hours ? ` · ${d.smart.power_on_hours.toLocaleString()} h` : ""}
                    </dd>
                  </>
                )}
                <dt>Last seen</dt>
                <dd>{ago(d.last_seen_at)}</dd>
              </dl>
              {d.last_error && d.status !== "online" && <div className="err small">{d.last_error}</div>}
              <div className="muted mono tiny" title={d.path}>
                {d.path}
              </div>
            </div>
          );
        })}
      </div>

      {o.rebuilds.length > 0 && (
        <>
          <h2>Rebuilds</h2>
          {o.rebuilds.map((r) => (
            <div key={r.id} className="card rebuild">
              <div>
                Disk {r.source_disk_id} → {r.target_disk_id ?? "spare capacity"} · <strong>{r.status}</strong>
              </div>
              <div className="meter">
                <span style={{ width: `${r.total_shards ? (100 * r.done_shards) / r.total_shards : 100}%` }} />
              </div>
              <div className="muted small">
                {r.done_shards.toLocaleString()} / {r.total_shards.toLocaleString()} shards · {bytes(r.bytes_written)}{" "}
                written · {bytes(r.rate_bytes_s)}/s
                {r.eta_s ? ` · ETA ${Math.round(r.eta_s / 60)} min` : ""}
                {r.failed_shards ? <span className="err"> · {r.failed_shards} failed</span> : null}
              </div>
            </div>
          ))}
        </>
      )}

      <h2>Scrub history</h2>
      <p className="muted small">
        Every shard's BLAKE2b checksum is re-verified at least every {o.scrub.interval_days} days, rate-limited to{" "}
        {bytes(o.scrub.rate_bytes_s)}/s; corrupted or missing shards are rebuilt from the survivors.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Started</th>
            <th>Status</th>
            <th className="num">Stripes</th>
            <th className="num">Shards verified</th>
            <th className="num">Read</th>
            <th className="num">Corrupt</th>
            <th className="num">Missing</th>
            <th className="num">Repaired</th>
          </tr>
        </thead>
        <tbody>
          {o.scrub.runs.map((r) => (
            <tr key={r.id}>
              <td>{dateTime(r.started_at)}</td>
              <td>{r.status}</td>
              <td className="num">{r.stripes_checked.toLocaleString()}</td>
              <td className="num">{r.shards_verified.toLocaleString()}</td>
              <td className="num">{bytes(r.bytes_read)}</td>
              <td className={`num${r.corrupt_found ? " err" : ""}`}>{r.corrupt_found}</td>
              <td className={`num${r.missing_found ? " err" : ""}`}>{r.missing_found}</td>
              <td className="num">{r.repaired}</td>
            </tr>
          ))}
          {o.scrub.runs.length === 0 && (
            <tr>
              <td colSpan={8} className="muted">
                No scrub runs yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <h2>Devices</h2>
      <table className="table">
        <thead>
          <tr>
            <th>Device</th>
            <th>Platform</th>
            <th className="num">Backed-up items</th>
            <th>Last sync</th>
          </tr>
        </thead>
        <tbody>
          {o.devices.map((d) => (
            <tr key={d.id}>
              <td>{d.name}</td>
              <td>{d.platform}</td>
              <td className="num">{d.items.toLocaleString()}</td>
              <td>{ago(d.last_sync_at)}</td>
            </tr>
          ))}
          {o.devices.length === 0 && (
            <tr>
              <td colSpan={4} className="muted">
                No devices yet — sign in from the iOS app.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
