"""/api/auth: login, refresh (rotating), logout, whoami."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from ..auth import AuthError
from .deps import ACCESS_COOKIE, REFRESH_COOKIE, require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str
    client: str = "web"  # web | ios


class RefreshBody(BaseModel):
    refresh_token: str | None = None


def _set_cookies(request: Request, response: Response, tokens: dict) -> None:
    cfg = request.app.state.svc.config
    response.set_cookie(ACCESS_COOKIE, tokens["access_token"], max_age=cfg.access_token_ttl_s, httponly=True,
                        samesite="strict", secure=cfg.cookie_secure, path="/")
    response.set_cookie(REFRESH_COOKIE, tokens["refresh_token"], max_age=cfg.refresh_token_ttl_s, httponly=True,
                        samesite="strict", secure=cfg.cookie_secure, path="/api/auth")


@router.get("/status")
def status(request: Request) -> dict:
    return {"password_set": request.app.state.auth.password_set()}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    auth = request.app.state.auth
    ip = request.client.host if request.client else "?"
    if auth.rate_limited(ip):
        raise HTTPException(429, "too many login attempts; wait a minute")
    try:
        tokens = auth.login(body.password, body.client)
    except AuthError as e:
        raise HTTPException(401, str(e)) from e
    if body.client == "web":
        _set_cookies(request, response, tokens)
        return {"ok": True, "expires_in": tokens["expires_in"]}
    return tokens


@router.post("/refresh")
def refresh(request: Request, response: Response, body: RefreshBody | None = None) -> dict:
    token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(401, "no refresh token")
    try:
        tokens = request.app.state.auth.refresh(token)
    except AuthError as e:
        response.delete_cookie(ACCESS_COOKIE, path="/")
        response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
        raise HTTPException(401, str(e)) from e
    if body is None or body.refresh_token is None:
        _set_cookies(request, response, tokens)
        return {"ok": True, "expires_in": tokens["expires_in"]}
    return tokens


@router.post("/logout")
def logout(request: Request, response: Response, body: RefreshBody | None = None) -> dict:
    token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    if token:
        request.app.state.auth.logout(token)
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(require_user)) -> dict:
    return {"user": user["sub"], "client": user.get("cli"), "expires": user["exp"]}
