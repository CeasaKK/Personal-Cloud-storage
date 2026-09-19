// Map of geotagged photos. Clusters come from the server quadtree for the visible
// viewport and zoom, so the browser never receives every point.

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, FileSummary, thumbUrl } from "../api";
import { Viewer } from "../components/Viewer";

export function MapPage() {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [viewer, setViewer] = useState<FileSummary[] | null>(null);
  const [viewerIndex, setViewerIndex] = useState(0);
  const [params] = useSearchParams();

  useEffect(() => {
    if (!el.current || map.current) return;
    const lat = Number(params.get("lat") ?? 20);
    const lon = Number(params.get("lon") ?? 78);
    const z = Number(params.get("z") ?? 3);
    const m = L.map(el.current, { worldCopyJump: true, zoomControl: true }).setView([lat, lon], z);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(m);
    layer.current = L.layerGroup().addTo(m);
    map.current = m;

    let seq = 0;
    const refresh = async () => {
      const b = m.getBounds();
      const mine = ++seq;
      const west = ((b.getWest() + 540) % 360) - 180;
      const east = ((b.getEast() + 540) % 360) - 180;
      const spansWorld = b.getEast() - b.getWest() >= 360;
      const res = await api.clusters({
        west: spansWorld ? -180 : west,
        east: spansWorld ? 180 : east,
        south: Math.max(b.getSouth(), -85),
        north: Math.min(b.getNorth(), 85),
        zoom: m.getZoom(),
      });
      if (mine !== seq) return; // a newer viewport already answered
      setTotal(res.total);
      const g = layer.current!;
      g.clearLayers();
      for (const c of res.clusters) {
        const single = c.count === 1 && c.file_id;
        const size = single ? 48 : Math.min(28 + Math.log2(c.count) * 6, 72);
        const html = single
          ? `<img src="${thumbUrl(c.file_id!)}" alt="" />`
          : `${c.file_id ? `<img src="${thumbUrl(c.file_id)}" alt="" />` : ""}<span>${c.count}</span>`;
        const marker = L.marker([c.lat, c.lon], {
          icon: L.divIcon({ className: `map-pin${single ? " single" : ""}`, html, iconSize: [size, size] }),
          title: `${c.count} item${c.count > 1 ? "s" : ""}`,
        });
        marker.on("click", async () => {
          if (single || m.getZoom() >= 17) {
            const pad = 0.0005 * Math.max(1, 18 - m.getZoom());
            const r = await api.search({
              lat: c.lat,
              lon: c.lon,
              radius_km: Math.max(0.05, pad * 111),
              limit: 200,
            });
            if (r.items.length) {
              setViewerIndex(0);
              setViewer(r.items);
            }
          } else {
            m.setView([c.lat, c.lon], Math.min(m.getZoom() + 3, 18));
          }
        });
        g.addLayer(marker);
      }
    };
    m.on("moveend", refresh);
    refresh();
    return () => {
      m.remove();
      map.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="map-page">
      <div ref={el} className="map" />
      {total !== null && <div className="map-count">{total.toLocaleString()} geotagged items in view</div>}
      {viewer && (
        <Viewer
          items={viewer}
          index={viewerIndex}
          onIndex={setViewerIndex}
          onClose={() => setViewer(null)}
          onChanged={(f, removed) =>
            setViewer((xs) => (removed ? xs!.filter((x) => x.id !== f.id) : xs!.map((x) => (x.id === f.id ? f : x))))
          }
        />
      )}
    </div>
  );
}
