// Typed client for the cloudstore API. Auth is HttpOnly cookies set by /api/auth/login;
// every request carries X-Requested-With (the server's CSRF guard for cookie auth), and a
// 401 triggers one transparent refresh (rotating refresh token) before giving up.

export interface FileSummary {
  id: number;
  sha256: string;
  kind: "image" | "video" | "other";
  mime: string;
  tier: "media" | "nonmedia";
  filename: string;
  size: number;
  capture_ts: number;
  width: number | null;
  height: number | null;
  duration: number | null;
  favorite: boolean;
  dup_group: number | null;
  has_location: boolean;
  trashed_at: number | null;
}

export interface FileDetail extends FileSummary {
  upload_ts: number;
  camera_make: string | null;
  camera_model: string | null;
  lat: number | null;
  lon: number | null;
  exif: Record<string, unknown>;
  albums?: { id: number; name: string }[];
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface Album {
  id: number;
  name: string;
  count: number;
  cover: number | null;
  updated_at: number;
}

export interface Cluster {
  lat: number;
  lon: number;
  count: number;
  file_id: number | null;
}

export interface DupGroup {
  group_id: number;
  members: (FileSummary & { sharpness: number | null })[];
  suggested_keep: number;
  reclaimable_bytes: number;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

let refreshing: Promise<boolean> | null = null;
async function tryRefresh(): Promise<boolean> {
  refreshing ??= fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "X-Requested-With": "fetch" },
    credentials: "same-origin",
  })
    .then((r) => r.ok)
    .finally(() => setTimeout(() => (refreshing = null), 0));
  return refreshing;
}

export async function request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-Requested-With", "fetch");
  if (init.body && typeof init.body === "string") headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (res.status === 401 && retry && !path.startsWith("/api/auth/")) {
    if (await tryRefresh()) return request<T>(path, init, false);
    onUnauthorized();
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = body.detail ?? msg;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
}

export const api = {
  status: () => request<{ password_set: boolean }>("/api/auth/status"),
  login: (password: string) => post<{ ok: boolean }>("/api/auth/login", { password, client: "web" }),
  logout: () => post("/api/auth/logout"),
  me: () => request<{ user: string }>("/api/auth/me"),

  timeline: (p: { cursor?: string | null; kind?: string; favorite?: boolean; album?: number; limit?: number }) =>
    request<Page<FileSummary>>(`/api/timeline${qs(p)}`),
  file: (id: number) => request<FileDetail>(`/api/files/${id}`),
  setFavorite: (id: number, favorite: boolean) =>
    request<FileDetail>(`/api/files/${id}`, { method: "PATCH", body: JSON.stringify({ favorite }) }),
  trash: (ids: number[]) => post<{ trashed: number }>("/api/files/trash", { ids }),
  restore: (ids: number[]) => post<{ restored: number }>("/api/files/restore", { ids }),
  listTrash: () => request<{ items: FileSummary[]; retention_days: number }>("/api/trash"),
  purge: (ids?: number[]) => post<{ purged: number }>("/api/trash/purge", ids ? { ids } : undefined),
  files: (cursor?: string | null) => request<Page<FileSummary>>(`/api/files${qs({ tier: "nonmedia", cursor })}`),

  albums: () => request<{ albums: Album[] }>("/api/albums"),
  album: (id: number) => request<{ id: number; name: string }>(`/api/albums/${id}`),
  createAlbum: (name: string) => post<{ id: number; name: string }>("/api/albums", { name }),
  renameAlbum: (id: number, name: string) =>
    request(`/api/albums/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  deleteAlbum: (id: number) => request(`/api/albums/${id}`, { method: "DELETE" }),
  addToAlbum: (id: number, ids: number[]) => post(`/api/albums/${id}/items`, { ids }),
  removeFromAlbum: (id: number, ids: number[]) => post(`/api/albums/${id}/items/remove`, { ids }),

  search: (p: Record<string, string | number | undefined>) => request<{ items: FileSummary[] }>(`/api/search${qs(p)}`),
  facets: () =>
    request<{
      cameras: { make: string | null; model: string; n: number }[];
      date_range: { lo: number | null; hi: number | null; n: number };
    }>("/api/search/facets"),
  clusters: (b: { west: number; south: number; east: number; north: number; zoom: number }) =>
    request<{ clusters: Cluster[]; total: number }>(`/api/map/clusters${qs(b)}`),

  duplicates: () => request<{ groups: DupGroup[]; reclaimable_bytes: number }>("/api/duplicates"),
  resolveDuplicates: (group: number, keep: number[]) => post(`/api/duplicates/${group}/resolve`, { keep }),
  dismissDuplicates: (group: number) => post(`/api/duplicates/${group}/dismiss`),

  overview: () => request<StorageOverview>("/api/storage/overview"),
  scrubNow: () => post("/api/storage/scrub"),
  healthCheck: () => post("/api/storage/health-check"),
  ingestJobs: () => request<{ jobs: IngestJob[] }>("/api/storage/ingest"),
};

export const thumbUrl = (id: number) => `/api/files/${id}/thumb`;
export const proxyUrl = (id: number) => `/api/files/${id}/proxy`;
export const originalUrl = (id: number, download = false) =>
  `/api/files/${id}/original${download ? "?download=true" : ""}`;

export interface IngestJob {
  id: number;
  sha256: string;
  state: string;
  attempts: number;
  error: string | null;
  file_id: number | null;
  filename: string | null;
  created_at: number;
}

export interface DiskInfo {
  id: number;
  uuid: string;
  label: string;
  path: string;
  status: string;
  error_count: number;
  last_error: string | null;
  capacity_bytes: number | null;
  free_bytes: number | null;
  last_seen_at: number | null;
  shards: number;
  smart: { passed?: boolean; temperature?: number; power_on_hours?: number; reallocated?: number } | null;
}

export interface ScrubRun {
  id: number;
  started_at: number;
  finished_at: number | null;
  status: string;
  stripes_checked: number;
  shards_verified: number;
  bytes_read: number;
  corrupt_found: number;
  missing_found: number;
  repaired: number;
  unrecoverable: number;
}

export interface RebuildTask {
  id: number;
  source_disk_id: number;
  target_disk_id: number | null;
  status: string;
  total_shards: number;
  done_shards: number;
  failed_shards: number;
  bytes_written: number;
  rate_bytes_s: number;
  eta_s: number | null;
}

export interface StorageOverview {
  capacity: {
    profile: { k: number; m: number; mode: string };
    raw_bytes: number;
    usable_bytes: number;
    free_raw_bytes: number;
    free_usable_bytes: number;
    logical_stored_bytes: number;
    physical_stored_bytes: number;
    disks_tolerated: number;
  };
  stripes: Record<string, number>;
  disks: DiskInfo[];
  tiers: {
    media: { n: number; bytes: number };
    nonmedia: { n: number; bytes: number };
    nonmedia_store: {
      logical_bytes: number;
      unique_chunk_bytes: number;
      unique_chunks: number;
      dedup_ratio: number | null;
      sealed_raw_bytes: number;
      sealed_compressed_bytes: number;
      compression_ratio: number | null;
      open_batch_bytes: number;
      bloom: { bits: number; k: number; items: number; expected_fpr: number; negative: number; positive: number; false_positive: number };
    };
  };
  scrub: { runs: ScrubRun[]; oldest_verified_at: number | null; rate_bytes_s: number; interval_days: number };
  rebuilds: RebuildTask[];
  ingest: Record<string, number>;
  thumb_cache: { items: number; used_bytes: number; budget_bytes: number; hit_rate: number | null };
  gf_backend: string;
  devices: { id: string; name: string; platform: string; last_sync_at: number | null; items: number }[];
}
