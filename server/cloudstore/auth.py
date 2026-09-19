"""Single-user authentication (TDD §11).

scrypt password hash in ``settings``; HS256 access JWTs (15 min); opaque
refresh tokens (30 days) stored hashed and rotated on every use — presenting an
already-rotated token revokes its whole family (token theft detection).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
import uuid
from collections import defaultdict, deque

import jwt

from .config import Config
from .db.database import Database

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


class AuthError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(),
                                          base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self.secret = self._load_secret()
        self._attempts: dict[str, deque] = defaultdict(deque)

    def _load_secret(self) -> bytes:
        p = self.config.secret_path
        if not p.exists():
            p.write_bytes(secrets.token_bytes(32))
            p.chmod(0o600)
        return p.read_bytes()

    # -- password -------------------------------------------------------------
    def password_set(self) -> bool:
        return self.db.get_setting("password_hash") is not None

    def set_password(self, password: str) -> None:
        if len(password) < 12:
            raise AuthError("password must be at least 12 characters")
        self.db.set_setting("password_hash", hash_password(password))
        self.db.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE revoked_at IS NULL", (time.time(),))

    def rate_limited(self, ip: str, limit: int = 5, window: float = 60.0) -> bool:
        q = self._attempts[ip]
        now = time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False

    def login(self, password: str, client: str) -> dict:
        stored = self.db.get_setting("password_hash")
        if stored is None:
            raise AuthError("no password configured: run `cloudstore set-password` on the server")
        if not verify_password(password, stored):
            raise AuthError("invalid password")
        return self._issue(client, family=uuid.uuid4().hex)

    # -- tokens ---------------------------------------------------------------
    def _issue(self, client: str, family: str) -> dict:
        now = int(time.time())
        access = jwt.encode({"sub": "owner", "iat": now, "exp": now + self.config.access_token_ttl_s,
                             "cli": client}, self.secret, algorithm="HS256")
        refresh = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO refresh_tokens(token_hash, family, client, created_at, expires_at) VALUES(?,?,?,?,?)",
            (hashlib.sha256(refresh.encode()).hexdigest(), family, client, now, now + self.config.refresh_token_ttl_s))
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
                "expires_in": self.config.access_token_ttl_s}

    def refresh(self, token: str) -> dict:
        th = hashlib.sha256(token.encode()).hexdigest()
        row = self.db.one("SELECT * FROM refresh_tokens WHERE token_hash = ?", (th,))
        now = time.time()
        if row is None or row["expires_at"] < now:
            raise AuthError("invalid refresh token")
        if row["revoked_at"] is not None:
            # reuse of a rotated token: assume theft, kill the family
            self.db.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE family = ? AND revoked_at IS NULL",
                            (now, row["family"]))
            raise AuthError("refresh token reuse detected; session revoked")
        issued = self._issue(row["client"], row["family"])
        new_id = self.db.scalar("SELECT id FROM refresh_tokens WHERE token_hash = ?",
                                (hashlib.sha256(issued["refresh_token"].encode()).hexdigest(),))
        self.db.execute("UPDATE refresh_tokens SET revoked_at = ?, replaced_by = ? WHERE id = ?", (now, new_id, row["id"]))
        return issued

    def logout(self, token: str) -> None:
        th = hashlib.sha256(token.encode()).hexdigest()
        fam = self.db.scalar("SELECT family FROM refresh_tokens WHERE token_hash = ?", (th,))
        if fam:
            self.db.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE family = ? AND revoked_at IS NULL",
                            (time.time(), fam))

    def verify_access(self, token: str) -> dict:
        try:
            return jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.PyJWTError as e:
            raise AuthError(str(e)) from e
