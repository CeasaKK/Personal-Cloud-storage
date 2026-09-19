// Near-duplicate review, non-media files, trash.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, DupGroup, FileSummary, originalUrl, thumbUrl } from "../api";
import { Icon } from "../components/Icon";
import { useUploads } from "../components/Uploads";
import { bytes, dateTime } from "../format";

export function DuplicatesPage() {
  const [groups, setGroups] = useState<DupGroup[] | null>(null);
  const [total, setTotal] = useState(0);
  const [keep, setKeep] = useState<Record<number, Set<number>>>({});
  const load = useCallback(() => {
    api.duplicates().then((r) => {
      setGroups(r.groups);
      setTotal(r.reclaimable_bytes);
      setKeep(Object.fromEntries(r.groups.map((g) => [g.group_id, new Set([g.suggested_keep])])));
    });
  }, []);
  useEffect(load, [load]);

  const toggle = (g: number, id: number) =>
    setKeep((k) => {
      const s = new Set(k[g]);
      if (s.has(id)) s.delete(id);
      else s.add(id);
      return { ...k, [g]: s };
    });

  return (
    <div className="page">
      <div className="page-title">
        Near-duplicates
        <span className="muted small">{groups ? `${groups.length} groups · ${bytes(total)} reclaimable` : ""}</span>
      </div>
      <p className="muted">
        Burst shots and re-saved copies grouped by perceptual hash. The suggested keeper (highest resolution, then
        sharpest) is pre-selected; the rest go to Trash, recoverable for 30 days.
      </p>
      {groups?.length === 0 && <div className="empty">No near-duplicates found.</div>}
      {groups?.map((g) => (
        <section key={g.group_id} className="dup-group">
          <div className="dup-strip">
            {g.members.map((m) => {
              const kept = keep[g.group_id]?.has(m.id);
              return (
                <button
                  key={m.id}
                  className={`dup-item${kept ? " kept" : ""}`}
                  onClick={() => toggle(g.group_id, m.id)}
                  aria-pressed={kept}
                  title={kept ? "Will keep" : "Will move to trash"}
                >
                  <img src={thumbUrl(m.id)} alt={m.filename} loading="lazy" />
                  <span className="dup-meta">
                    {m.width}×{m.height} · {bytes(m.size)}
                    {m.id === g.suggested_keep && <em> · suggested</em>}
                  </span>
                  {kept && (
                    <span className="keep-badge">
                      <Icon name="check" size={16} /> keep
                    </span>
                  )}
                </button>
              );
            })}
          </div>
          <div className="dup-actions">
            <span className="muted">{bytes(g.reclaimable_bytes)} reclaimable</span>
            <div className="spacer" />
            <button className="btn" onClick={() => api.dismissDuplicates(g.group_id).then(load)}>
              Keep all
            </button>
            <button
              className="btn primary"
              disabled={!keep[g.group_id]?.size}
              onClick={() => api.resolveDuplicates(g.group_id, [...keep[g.group_id]]).then(load)}
            >
              Keep selected, trash {g.members.length - (keep[g.group_id]?.size ?? 0)}
            </button>
          </div>
        </section>
      ))}
    </div>
  );
}

export function FilesPage() {
  const [items, setItems] = useState<FileSummary[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const { add, version } = useUploads();
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);

  useEffect(() => {
    api.files().then((r) => {
      setItems(r.items);
      setCursor(r.next_cursor);
    });
  }, [version]);

  return (
    <div
      className={`page${drag ? " dragging" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        add(e.dataTransfer.files);
      }}
    >
      <div className="page-title">
        Files
        <div className="spacer" />
        <button className="btn primary" onClick={() => input.current?.click()}>
          <Icon name="upload" size={18} /> Upload files
        </button>
        <input ref={input} type="file" multiple hidden onChange={(e) => e.target.files && add(e.target.files)} />
      </div>
      <p className="muted">
        Documents, notes, code and RAW files live in the non-media tier: content-defined chunking removes duplicate
        chunks across files, then batches are compressed together with zstd and stored erasure-coded. Drop files
        anywhere on this page.
      </p>
      {items?.length === 0 && <div className="empty">No files yet.</div>}
      {!!items?.length && (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Type</th>
              <th className="num">Size</th>
              <th>Uploaded</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((f) => (
              <tr key={f.id}>
                <td>{f.filename}</td>
                <td className="muted">{f.mime}</td>
                <td className="num">{bytes(f.size)}</td>
                <td className="muted">{dateTime(f.capture_ts)}</td>
                <td className="row-actions">
                  <a className="icon-btn" href={originalUrl(f.id, true)} aria-label={`Download ${f.filename}`}>
                    <Icon name="download" size={20} />
                  </a>
                  <button
                    className="icon-btn"
                    aria-label={`Trash ${f.filename}`}
                    onClick={async () => {
                      await api.trash([f.id]);
                      setItems((xs) => xs!.filter((x) => x.id !== f.id));
                    }}
                  >
                    <Icon name="trash" size={20} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {cursor && (
        <button
          className="btn"
          onClick={() =>
            api.files(cursor).then((r) => {
              setItems((xs) => [...(xs ?? []), ...r.items]);
              setCursor(r.next_cursor);
            })
          }
        >
          Load more
        </button>
      )}
    </div>
  );
}

export function TrashPage() {
  const [items, setItems] = useState<FileSummary[] | null>(null);
  const [days, setDays] = useState(30);
  const load = () =>
    api.listTrash().then((r) => {
      setItems(r.items);
      setDays(r.retention_days);
    });
  useEffect(() => {
    load();
  }, []);

  return (
    <div className="page">
      <div className="page-title">
        Trash
        <div className="spacer" />
        {!!items?.length && (
          <button
            className="btn danger"
            onClick={async () => {
              if (confirm(`Permanently delete ${items.length} item(s)? This cannot be undone.`)) {
                await api.purge();
                load();
              }
            }}
          >
            Empty trash
          </button>
        )}
      </div>
      <p className="muted">
        Items are permanently deleted after {days} days. Deleted items are remembered by hash, so a phone that still has
        them will not upload them again.
      </p>
      {items?.length === 0 && <div className="empty">Trash is empty.</div>}
      <div className="trash-grid">
        {items?.map((f) => (
          <div key={f.id} className="trash-item">
            {f.tier === "media" ? (
              <img src={thumbUrl(f.id)} alt={f.filename} loading="lazy" />
            ) : (
              <div className="file-tile">
                <Icon name="files" size={40} />
                <span>{f.filename}</span>
              </div>
            )}
            <div className="trash-actions">
              <button className="btn small" onClick={() => api.restore([f.id]).then(load)}>
                <Icon name="restore" size={16} /> Restore
              </button>
              <button
                className="btn small danger"
                onClick={() => confirm("Delete forever?") && api.purge([f.id]).then(load)}
              >
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
