// Paged photo list + selection actions + viewer. Used by Photos, Favourites, Albums, Search.

import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { Album, api, FileSummary, Page } from "../api";
import { Icon } from "./Icon";
import { PhotoGrid } from "./PhotoGrid";
import { useUploads } from "./Uploads";
import { Viewer } from "./Viewer";

interface Props {
  load: (cursor: string | null) => Promise<Page<FileSummary>>;
  deps?: unknown[];
  title?: ReactNode;
  empty?: ReactNode;
  extraActions?: (ids: number[], clear: () => void, reload: () => void) => ReactNode;
  groupByDay?: boolean;
}

export function MediaBrowser({ load, deps = [], title, empty, extraActions, groupByDay = true }: Props) {
  const [items, setItems] = useState<FileSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [open, setOpen] = useState<number | null>(null);
  const [albumPicker, setAlbumPicker] = useState(false);
  const busy = useRef(false);
  const { version } = useUploads();

  const reload = useCallback(async () => {
    busy.current = true;
    setLoading(true);
    try {
      const page = await load(null);
      setItems(page.items);
      setCursor(page.next_cursor);
      setHasMore(!!page.next_cursor);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      busy.current = false;
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    setSelected(new Set());
    reload();
  }, [reload, version]);

  const loadMore = useCallback(async () => {
    if (busy.current || !cursor) return;
    busy.current = true;
    try {
      const page = await load(cursor);
      setItems((xs) => [...xs, ...page.items]);
      setCursor(page.next_cursor);
      setHasMore(!!page.next_cursor);
    } finally {
      busy.current = false;
    }
  }, [cursor, load]);

  const toggle = (id: number) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  const clear = () => setSelected(new Set());
  const ids = [...selected];

  async function trashSelected() {
    await api.trash(ids);
    setItems((xs) => xs.filter((x) => !selected.has(x.id)));
    clear();
  }

  async function favouriteSelected() {
    const allFav = items.filter((x) => selected.has(x.id)).every((x) => x.favorite);
    await Promise.all(ids.map((id) => api.setFavorite(id, !allFav)));
    setItems((xs) => xs.map((x) => (selected.has(x.id) ? { ...x, favorite: !allFav } : x)));
    clear();
  }

  return (
    <>
      {selected.size > 0 ? (
        <div className="selection-bar">
          <button className="icon-btn" onClick={clear} aria-label="Clear selection">
            <Icon name="close" />
          </button>
          <strong>{selected.size} selected</strong>
          <div className="spacer" />
          {extraActions?.(ids, clear, reload)}
          <button className="icon-btn" onClick={() => setAlbumPicker(true)} title="Add to album">
            <Icon name="plus" />
          </button>
          <button className="icon-btn" onClick={favouriteSelected} title="Favourite">
            <Icon name="star" />
          </button>
          <button className="icon-btn" onClick={trashSelected} title="Move to trash">
            <Icon name="trash" />
          </button>
        </div>
      ) : (
        title && <div className="page-title">{title}</div>
      )}
      {error && <div className="banner error">{error}</div>}
      {!loading && items.length === 0 && !error ? (
        <div className="empty">{empty ?? "Nothing here yet."}</div>
      ) : (
        <PhotoGrid
          items={items}
          selected={selected}
          onToggle={toggle}
          onOpen={setOpen}
          onLoadMore={loadMore}
          hasMore={hasMore}
          groupByDay={groupByDay}
        />
      )}
      {open !== null && (
        <Viewer
          items={items}
          index={open}
          onIndex={setOpen}
          onClose={() => setOpen(null)}
          onChanged={(f, removed) => {
            if (removed) {
              setItems((xs) => xs.filter((x) => x.id !== f.id));
              if (open >= items.length - 1) setOpen(items.length > 1 ? open - 1 : null);
            } else setItems((xs) => xs.map((x) => (x.id === f.id ? f : x)));
          }}
        />
      )}
      {albumPicker && (
        <AlbumPicker
          onClose={() => setAlbumPicker(false)}
          onPick={async (a) => {
            await api.addToAlbum(a.id, ids);
            setAlbumPicker(false);
            clear();
          }}
        />
      )}
    </>
  );
}

function AlbumPicker({ onClose, onPick }: { onClose: () => void; onPick: (a: Album) => void }) {
  const [albums, setAlbums] = useState<Album[]>([]);
  const [name, setName] = useState("");
  useEffect(() => {
    api.albums().then((r) => setAlbums(r.albums));
  }, []);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Add to album">
        <h2>Add to album</h2>
        <form
          className="inline-form"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!name.trim()) return;
            const a = await api.createAlbum(name.trim());
            onPick({ ...a, count: 0, cover: null, updated_at: Date.now() / 1000 });
          }}
        >
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New album name" autoFocus />
          <button className="btn primary" type="submit">
            Create
          </button>
        </form>
        <ul className="picker-list">
          {albums.map((a) => (
            <li key={a.id}>
              <button onClick={() => onPick(a)}>
                <span>{a.name}</span>
                <span className="muted">{a.count} items</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
