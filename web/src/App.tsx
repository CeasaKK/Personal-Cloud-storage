import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError, setUnauthorizedHandler } from "./api";
import { Icon } from "./components/Icon";
import { UploadProvider, useUploads } from "./components/Uploads";
import { AlbumDetailPage, AlbumsPage, FavoritesPage, PhotosPage } from "./pages/Library";
import { DuplicatesPage, FilesPage, TrashPage } from "./pages/Manage";
import { MapPage } from "./pages/MapPage";
import { SearchPage } from "./pages/SearchPage";
import { StoragePage } from "./pages/StoragePage";

type AuthState = "loading" | "in" | "out";

export default function App() {
  const [auth, setAuth] = useState<AuthState>("loading");
  useEffect(() => {
    setUnauthorizedHandler(() => setAuth("out"));
    api
      .me()
      .then(() => setAuth("in"))
      .catch(() => setAuth("out"));
  }, []);

  if (auth === "loading") return <div className="splash"><div className="spinner" /></div>;
  if (auth === "out") return <Login onDone={() => setAuth("in")} />;
  return (
    <BrowserRouter>
      <UploadProvider>
        <Shell onLogout={() => setAuth("out")}>
          <Routes>
            <Route path="/" element={<PhotosPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/map" element={<MapPage />} />
            <Route path="/favorites" element={<FavoritesPage />} />
            <Route path="/albums" element={<AlbumsPage />} />
            <Route path="/albums/:id" element={<AlbumDetailPage />} />
            <Route path="/duplicates" element={<DuplicatesPage />} />
            <Route path="/files" element={<FilesPage />} />
            <Route path="/trash" element={<TrashPage />} />
            <Route path="/storage" element={<StoragePage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Shell>
      </UploadProvider>
    </BrowserRouter>
  );
}

const NAV = [
  { to: "/", icon: "photos", label: "Photos", end: true },
  { to: "/search", icon: "search", label: "Explore" },
  { to: "/map", icon: "map", label: "Places" },
  { to: "/favorites", icon: "star", label: "Favourites" },
  { to: "/albums", icon: "album", label: "Albums" },
  { to: "/duplicates", icon: "dupes", label: "Duplicates" },
  { to: "/files", icon: "files", label: "Files" },
  { to: "/trash", icon: "trash", label: "Trash" },
  { to: "/storage", icon: "storage", label: "Storage" },
];

function Shell({ children, onLogout }: { children: ReactNode; onLogout: () => void }) {
  const { add } = useUploads();
  const fileInput = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState("");
  const [navOpen, setNavOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  useEffect(() => setNavOpen(false), [loc.pathname]);

  return (
    <div className="shell">
      <header className="topbar">
        <button className="icon-btn menu" onClick={() => setNavOpen((o) => !o)} aria-label="Menu">
          <Icon name="menu" />
        </button>
        <a className="brand" href="/">
          <img src="/favicon.svg" alt="" width={28} height={28} />
          <span>Cloudstore</span>
        </a>
        <form
          className="search"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            nav(`/search?q=${encodeURIComponent(q)}`);
          }}
        >
          <Icon name="search" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search your photos" aria-label="Search" />
        </form>
        <div className="spacer" />
        <button className="btn upload" onClick={() => fileInput.current?.click()}>
          <Icon name="upload" size={20} />
          <span>Upload</span>
        </button>
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files) add(e.target.files);
            e.target.value = "";
          }}
        />
        <button className="icon-btn" aria-label="Sign out" title="Sign out" onClick={() => api.logout().finally(onLogout)}>
          <Icon name="logout" />
        </button>
      </header>
      <nav className={`sidebar${navOpen ? " open" : ""}`}>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? "active" : "")}>
            <Icon name={n.icon} />
            <span>{n.label}</span>
          </NavLink>
        ))}
      </nav>
      <main className="content">{children}</main>
    </div>
  );
}

function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [configured, setConfigured] = useState(true);
  useEffect(() => {
    api.status().then((s) => setConfigured(s.password_set)).catch(() => {});
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.login(password);
      onDone();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not reach the server");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="login-card">
        <img src="/favicon.svg" alt="" width={56} height={56} />
        <h1>Cloudstore</h1>
        <p className="muted">Your photos, on your own hardware.</p>
        {!configured && (
          <div className="banner warn">
            No password is set yet. On the server run <code>cloudstore set-password</code>.
          </div>
        )}
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoFocus
          autoComplete="current-password"
          aria-label="Password"
        />
        {err && <div className="err">{err}</div>}
        <button className="btn primary" disabled={busy || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
