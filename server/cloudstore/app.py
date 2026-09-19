"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import auth_routes, files, library, storage, sync, tus
from .auth import Auth
from .config import Config
from .services import Services


def create_app(config: Config | None = None, services: Services | None = None) -> FastAPI:
    config = config or Config()
    svc = services or Services(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.startup()
        try:
            yield
        finally:
            svc.shutdown()

    app = FastAPI(title="cloudstore", version=__version__, lifespan=lifespan)
    app.state.svc = svc
    app.state.auth = Auth(svc.db, config)
    for r in (auth_routes.router, tus.router, files.router, library.router, sync.router, storage.router):
        app.include_router(r)

    @app.get("/api/health")
    def health() -> dict:
        disks = svc.disks.list()
        return {"ok": True, "version": __version__,
                "disks": {s: sum(1 for d in disks if d.status == s) for s in {d.status for d in disks}}}

    dist = config.web_dist or (Path(__file__).resolve().parents[2] / "web" / "dist")
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
    return app


def main_app() -> FastAPI:  # uvicorn entry: `uvicorn cloudstore.app:main_app --factory`
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
