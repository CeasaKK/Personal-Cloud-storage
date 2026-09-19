// Photos timeline, Favourites, Albums (list + detail).

import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Album, api, thumbUrl } from "../api";
import { Icon } from "../components/Icon";
import { MediaBrowser } from "../components/MediaBrowser";

export function PhotosPage() {
  const load = useCallback((cursor: string | null) => api.timeline({ cursor }), []);
  return (
    <MediaBrowser
      load={load}
      empty={
        <>
          <h2>No photos yet</h2>
          <p>Open the iOS app to back up your camera roll, or drop files onto the Upload button.</p>
        </>
      }
    />
  );
}

export function FavoritesPage() {
  const load = useCallback((cursor: string | null) => api.timeline({ cursor, favorite: true }), []);
  return <MediaBrowser load={load} title="Favourites" empty="Star photos to see them here." />;
}

export function AlbumsPage() {
  const [albums, setAlbums] = useState<Album[] | null>(null);
  const [name, setName] = useState("");
  const nav = useNavigate();
  useEffect(() => {
    api.albums().then((r) => setAlbums(r.albums));
  }, []);
  return (
    <div className="page">
      <div className="page-title">
        Albums
        <form
          className="inline-form"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!name.trim()) return;
            const a = await api.createAlbum(name.trim());
            nav(`/albums/${a.id}`);
          }}
        >
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New album" />
          <button className="btn" type="submit">
            <Icon name="plus" size={18} /> Create
          </button>
        </form>
      </div>
      {albums && albums.length === 0 && <div className="empty">No albums yet. Select photos and use “Add to album”.</div>}
      <div className="album-grid">
        {albums?.map((a) => (
          <Link key={a.id} to={`/albums/${a.id}`} className="album-card">
            <div className="album-cover">
              {a.cover ? <img src={thumbUrl(a.cover)} alt="" loading="lazy" /> : <Icon name="album" size={48} />}
            </div>
            <div className="album-name">{a.name}</div>
            <div className="muted">{a.count} items</div>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function AlbumDetailPage() {
  const id = Number(useParams().id);
  const [name, setName] = useState("");
  const nav = useNavigate();
  useEffect(() => {
    api.album(id).then((a) => setName(a.name));
  }, [id]);
  const load = useCallback((cursor: string | null) => api.timeline({ cursor, album: id }), [id]);
  return (
    <MediaBrowser
      load={load}
      deps={[id]}
      title={
        <>
          <Link to="/albums" className="icon-btn" aria-label="Back to albums">
            <Icon name="back" />
          </Link>
          <input
            className="title-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => name.trim() && api.renameAlbum(id, name.trim())}
            aria-label="Album name"
          />
          <div className="spacer" />
          <button
            className="btn"
            onClick={async () => {
              if (confirm(`Delete album “${name}”? Photos stay in your library.`)) {
                await api.deleteAlbum(id);
                nav("/albums");
              }
            }}
          >
            Delete album
          </button>
        </>
      }
      empty="This album is empty. Select photos in the timeline and add them here."
      extraActions={(ids, clear, reload) => (
        <button
          className="btn"
          onClick={async () => {
            await api.removeFromAlbum(id, ids);
            clear();
            reload();
          }}
        >
          Remove from album
        </button>
      )}
    />
  );
}
