// Metadata search: filename, date range, camera, media type, and "near a place".

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { MediaBrowser } from "../components/MediaBrowser";

const toTs = (d: string) => (d ? new Date(d).getTime() / 1000 : undefined);

export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const [cameras, setCameras] = useState<{ make: string | null; model: string; n: number }[]>([]);
  useEffect(() => {
    api.facets().then((f) => setCameras(f.cameras));
  }, []);

  const q = params.get("q") ?? "";
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const camera = params.get("camera") ?? "";
  const kind = params.get("kind") ?? "";
  const lat = params.get("lat") ?? "";
  const lon = params.get("lon") ?? "";
  const radius = params.get("r") ?? "5";

  const set = (k: string, v: string) => {
    const n = new URLSearchParams(params);
    if (v) n.set(k, v);
    else n.delete(k);
    setParams(n, { replace: true });
  };

  const key = params.toString();
  const query = useMemo(
    () => ({
      q: q || undefined,
      date_from: toTs(from),
      date_to: to ? toTs(to)! + 86400 : undefined,
      camera: camera || undefined,
      kind: kind || undefined,
      lat: lat && lon ? Number(lat) : undefined,
      lon: lat && lon ? Number(lon) : undefined,
      radius_km: lat && lon ? Number(radius) : undefined,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key],
  );
  const load = useCallback(
    async (cursor: string | null) =>
      cursor ? { items: [], next_cursor: null } : { ...(await api.search(query)), next_cursor: null },
    [query],
  );

  function useMyLocation() {
    navigator.geolocation?.getCurrentPosition((p) => {
      const n = new URLSearchParams(params);
      n.set("lat", p.coords.latitude.toFixed(5));
      n.set("lon", p.coords.longitude.toFixed(5));
      setParams(n, { replace: true });
    });
  }

  return (
    <div className="page">
      <div className="filters">
        <label>
          Filename
          <input value={q} onChange={(e) => set("q", e.target.value)} placeholder="IMG_1234" />
        </label>
        <label>
          From
          <input type="date" value={from} onChange={(e) => set("from", e.target.value)} />
        </label>
        <label>
          To
          <input type="date" value={to} onChange={(e) => set("to", e.target.value)} />
        </label>
        <label>
          Camera
          <select value={camera} onChange={(e) => set("camera", e.target.value)}>
            <option value="">Any</option>
            {cameras.map((c) => (
              <option key={`${c.make}-${c.model}`} value={c.model}>
                {c.make ? `${c.make} ` : ""}
                {c.model} ({c.n})
              </option>
            ))}
          </select>
        </label>
        <label>
          Type
          <select value={kind} onChange={(e) => set("kind", e.target.value)}>
            <option value="">Photos & videos</option>
            <option value="image">Photos</option>
            <option value="video">Videos</option>
          </select>
        </label>
        <fieldset className="near">
          <legend>Near</legend>
          <input value={lat} onChange={(e) => set("lat", e.target.value)} placeholder="lat" inputMode="decimal" />
          <input value={lon} onChange={(e) => set("lon", e.target.value)} placeholder="lon" inputMode="decimal" />
          <select value={radius} onChange={(e) => set("r", e.target.value)} aria-label="Radius">
            {["1", "5", "25", "100", "500"].map((r) => (
              <option key={r} value={r}>
                {r} km
              </option>
            ))}
          </select>
          <button type="button" className="btn" onClick={useMyLocation}>
            Here
          </button>
        </fieldset>
      </div>
      <MediaBrowser load={load} deps={[load]} empty="No matches. Try widening the filters." />
    </div>
  );
}
