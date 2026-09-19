// Minimal tus 1.0.0 client for browser uploads (the iOS app has its own).
// Streams a SHA-256 over the file first (hash-wasm, incremental — works for multi-GB
// videos without loading them into memory), then uploads in 8 MiB PATCHes and resumes
// from the server's Upload-Offset after any failure.

import { createSHA256 } from "hash-wasm";

const CHUNK = 8 * 1024 * 1024;
const TUS = { "Tus-Resumable": "1.0.0", "X-Requested-With": "fetch" };

export type UploadPhase = "hashing" | "uploading" | "processing" | "done" | "error" | "skipped";

export interface UploadProgress {
  phase: UploadPhase;
  fraction: number;
  error?: string;
}

export async function sha256File(file: File, onProgress?: (f: number) => void): Promise<string> {
  const hasher = await createSHA256();
  hasher.init();
  for (let off = 0; off < file.size; off += CHUNK) {
    const buf = new Uint8Array(await file.slice(off, off + CHUNK).arrayBuffer());
    hasher.update(buf);
    onProgress?.(Math.min(1, (off + buf.length) / file.size));
  }
  return hasher.digest("hex");
}

const b64 = (s: string) => btoa(unescape(encodeURIComponent(s)));

export async function uploadFile(file: File, onProgress: (p: UploadProgress) => void): Promise<void> {
  try {
    const sha = await sha256File(file, (f) => onProgress({ phase: "hashing", fraction: f }));
    const known = await fetch(`/api/files/by-hash/${sha}`, { headers: TUS, credentials: "same-origin" });
    if (known.ok) {
      onProgress({ phase: "skipped", fraction: 1 });
      return;
    }
    const meta = [
      `filename ${b64(file.name)}`,
      `sha256 ${b64(sha)}`,
      `mimetype ${b64(file.type || "application/octet-stream")}`,
      `created_at ${b64(String(file.lastModified / 1000))}`,
    ].join(",");
    const create = await fetch("/api/tus", {
      method: "POST",
      credentials: "same-origin",
      headers: { ...TUS, "Upload-Length": String(file.size), "Upload-Metadata": meta },
    });
    if (create.status !== 201) throw new Error(`create failed: ${create.status}`);
    const location = create.headers.get("Location")!;
    let offset = Number(create.headers.get("Upload-Offset") ?? 0);
    let failures = 0;
    while (offset < file.size) {
      try {
        const res = await fetch(location, {
          method: "PATCH",
          credentials: "same-origin",
          headers: { ...TUS, "Upload-Offset": String(offset), "Content-Type": "application/offset+octet-stream" },
          body: file.slice(offset, offset + CHUNK),
        });
        if (res.status === 460) throw new Error("checksum mismatch — file changed during upload");
        if (res.status !== 204) throw new Error(`upload failed: ${res.status}`);
        offset = Number(res.headers.get("Upload-Offset"));
        failures = 0;
        onProgress({ phase: "uploading", fraction: offset / file.size });
      } catch (e) {
        if (++failures > 5 || String(e).includes("checksum")) throw e;
        await new Promise((r) => setTimeout(r, 1000 * 2 ** failures));
        // resume from wherever the server got to
        const head = await fetch(location, { method: "HEAD", headers: TUS, credentials: "same-origin" });
        offset = Number(head.headers.get("Upload-Offset") ?? offset);
      }
    }
    onProgress({ phase: "processing", fraction: 1 });
    for (let i = 0; i < 120; i++) {
      const r = await fetch(`/api/files/by-hash/${sha}`, { headers: TUS, credentials: "same-origin" });
      if (r.ok) {
        const body = await r.json();
        if (body.status === "stored") break;
        if (body.status === "failed") throw new Error(body.error ?? "ingest failed");
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    onProgress({ phase: "done", fraction: 1 });
  } catch (e) {
    onProgress({ phase: "error", fraction: 0, error: e instanceof Error ? e.message : String(e) });
  }
}
