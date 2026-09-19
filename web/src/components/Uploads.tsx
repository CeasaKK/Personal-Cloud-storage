// Upload queue (context) + floating progress panel.

import { createContext, ReactNode, useCallback, useContext, useRef, useState } from "react";
import { UploadProgress, uploadFile } from "../tus";
import { bytes } from "../format";
import { Icon } from "./Icon";

interface Item {
  id: number;
  file: File;
  progress: UploadProgress;
}

const Ctx = createContext<{ add: (files: FileList | File[]) => void; version: number }>({ add: () => {}, version: 0 });
export const useUploads = () => useContext(Ctx);

const CONCURRENCY = 2;

export function UploadProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Item[]>([]);
  const [open, setOpen] = useState(true);
  const [version, setVersion] = useState(0);
  const queue = useRef<Item[]>([]);
  const active = useRef(0);
  const nextId = useRef(1);

  const update = (id: number, progress: UploadProgress) =>
    setItems((xs) => xs.map((x) => (x.id === id ? { ...x, progress } : x)));

  const pump = useCallback(() => {
    while (active.current < CONCURRENCY && queue.current.length) {
      const item = queue.current.shift()!;
      active.current++;
      uploadFile(item.file, (p) => update(item.id, p)).finally(() => {
        active.current--;
        setVersion((v) => v + 1); // lets pages refresh as files land
        pump();
      });
    }
  }, []);

  const add = useCallback(
    (files: FileList | File[]) => {
      const fresh = Array.from(files).map((file) => ({
        id: nextId.current++,
        file,
        progress: { phase: "hashing" as const, fraction: 0 },
      }));
      queue.current.push(...fresh);
      setItems((xs) => [...fresh, ...xs].slice(0, 200));
      setOpen(true);
      pump();
    },
    [pump],
  );

  const doneCount = items.filter((i) => ["done", "skipped"].includes(i.progress.phase)).length;
  const errCount = items.filter((i) => i.progress.phase === "error").length;

  return (
    <Ctx.Provider value={{ add, version }}>
      {children}
      {items.length > 0 && (
        <div className={`upload-panel${open ? "" : " collapsed"}`}>
          <header onClick={() => setOpen((o) => !o)}>
            <strong>
              {doneCount + errCount < items.length
                ? `Uploading ${items.length - doneCount - errCount} item(s)`
                : `${doneCount} uploaded${errCount ? `, ${errCount} failed` : ""}`}
            </strong>
            <button
              className="icon-btn"
              aria-label="Dismiss"
              onClick={(e) => {
                e.stopPropagation();
                if (doneCount + errCount === items.length) setItems([]);
                else setOpen(false);
              }}
            >
              <Icon name="close" size={20} />
            </button>
          </header>
          {open && (
            <ul>
              {items.map((i) => (
                <li key={i.id}>
                  <div className="up-name" title={i.file.name}>
                    {i.file.name}
                  </div>
                  <div className="up-meta">
                    {i.progress.phase === "error" ? (
                      <span className="err">{i.progress.error}</span>
                    ) : i.progress.phase === "skipped" ? (
                      "already backed up"
                    ) : (
                      `${i.progress.phase} · ${bytes(i.file.size)}`
                    )}
                  </div>
                  <progress
                    max={1}
                    value={i.progress.phase === "done" || i.progress.phase === "skipped" ? 1 : i.progress.fraction}
                    className={i.progress.phase}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </Ctx.Provider>
  );
}
