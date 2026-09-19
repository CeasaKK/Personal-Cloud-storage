// Google-Photos-style justified grid grouped by day, with lazy thumbnails,
// infinite scroll and multi-select.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { FileSummary, thumbUrl } from "../api";
import { dayKey, dayLabel, duration } from "../format";
import { Icon } from "./Icon";

const TARGET_H = 200;
const GAP = 4;

interface Row {
  items: FileSummary[];
  height: number;
}

function aspect(f: FileSummary): number {
  if (f.width && f.height) return Math.min(Math.max(f.width / f.height, 0.4), 3);
  return 1;
}

export function justify(items: FileSummary[], width: number, target = TARGET_H): Row[] {
  const rows: Row[] = [];
  let cur: FileSummary[] = [];
  let sum = 0;
  for (const f of items) {
    cur.push(f);
    sum += aspect(f);
    const h = (width - GAP * (cur.length - 1)) / sum;
    if (h <= target) {
      rows.push({ items: cur, height: h });
      cur = [];
      sum = 0;
    }
  }
  if (cur.length) rows.push({ items: cur, height: Math.min(target, (width - GAP * (cur.length - 1)) / sum) });
  return rows;
}

interface Props {
  items: FileSummary[];
  onOpen: (index: number) => void;
  selected: Set<number>;
  onToggle: (id: number, additive: boolean) => void;
  onLoadMore?: () => void;
  hasMore?: boolean;
  groupByDay?: boolean;
}

export function PhotoGrid({ items, onOpen, selected, onToggle, onLoadMore, hasMore, groupByDay = true }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const sentinel = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.floor(e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!sentinel.current || !onLoadMore) return;
    const io = new IntersectionObserver(([e]) => e.isIntersecting && hasMore && onLoadMore(), { rootMargin: "1200px" });
    io.observe(sentinel.current);
    return () => io.disconnect();
  }, [onLoadMore, hasMore]);

  const index = useMemo(() => new Map(items.map((f, i) => [f.id, i])), [items]);
  const groups = useMemo(() => {
    if (!groupByDay) return [{ key: "all", label: "", items }];
    const out: { key: string; label: string; items: FileSummary[] }[] = [];
    for (const f of items) {
      const k = dayKey(f.capture_ts);
      if (!out.length || out[out.length - 1].key !== k) out.push({ key: k, label: dayLabel(f.capture_ts), items: [] });
      out[out.length - 1].items.push(f);
    }
    return out;
  }, [items, groupByDay]);

  const selecting = selected.size > 0;
  const target = width < 600 ? 120 : TARGET_H;

  return (
    <div ref={ref} className="grid">
      {width > 0 &&
        groups.map((g) => (
          <section key={g.key} className="day">
            {g.label && (
              <h3 className="day-label">
                <button
                  className="day-check"
                  aria-label={`Select ${g.label}`}
                  onClick={() => g.items.forEach((f) => !selected.has(f.id) && onToggle(f.id, true))}
                >
                  <Icon name="check" size={16} />
                </button>
                {g.label}
              </h3>
            )}
            {justify(g.items, width, target).map((row, ri) => (
              <div key={ri} className="row" style={{ height: row.height }}>
                {row.items.map((f) => {
                  const w = aspect(f) * row.height;
                  const isSel = selected.has(f.id);
                  return (
                    <div
                      key={f.id}
                      className={`tile${isSel ? " selected" : ""}${selecting ? " selecting" : ""}`}
                      style={{ width: w, height: row.height }}
                      onClick={(e) => (selecting || e.shiftKey || e.metaKey ? onToggle(f.id, true) : onOpen(index.get(f.id)!))}
                    >
                      <img src={thumbUrl(f.id)} loading="lazy" decoding="async" alt={f.filename} draggable={false} />
                      {f.kind === "video" && (
                        <span className="badge video">
                          {duration(f.duration)} <Icon name="play" size={14} />
                        </span>
                      )}
                      {f.favorite && (
                        <span className="badge fav">
                          <Icon name="starFilled" size={16} />
                        </span>
                      )}
                      <button
                        className="tile-check"
                        aria-label={isSel ? "Deselect" : "Select"}
                        onClick={(e) => {
                          e.stopPropagation();
                          onToggle(f.id, true);
                        }}
                      >
                        <Icon name="check" size={16} />
                      </button>
                    </div>
                  );
                })}
              </div>
            ))}
          </section>
        ))}
      <div ref={sentinel} className="sentinel">
        {hasMore && <div className="spinner" />}
      </div>
    </div>
  );
}
