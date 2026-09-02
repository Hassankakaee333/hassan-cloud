"""Token authentication — hashed, revocable, no secrets in logs."""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

from fastapi import Header, HTTPException

if TYPE_CHECKING:
    from ..storage.repository import DatabaseRepository

from ..config import BOOTSTRAP_TOKEN, DEV_TOKEN, ENV


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TokenService:
    def __init__(self, repo: "DatabaseRepository") -> None:
        self.repo = repo

    def bootstrap(self, new_id, now_ms) -> str | None:
        """Seed tokens from env on first run. Returns raw bootstrap token if generated."""
        tokens = self.repo.list_tokens()
        if tokens:
            return None
        raw = BOOTSTRAP_TOKEN or DEV_TOKEN
        if not raw:
            if ENV == "development":
                raw = secrets.token_urlsafe(32)
            else:
                return None
        self.repo.insert_token(new_id(), hash_token(raw), "bootstrap", None, now_ms())
        return raw

    def create_token(self, label: str, device_id: str | None, new_id, now_ms) -> tuple[str, str]:
        raw = secrets.token_urlsafe(32)
        tid = new_id()
        self.repo.insert_token(tid, hash_token(raw), label, device_id, now_ms())
        return tid, raw

    def verify(self, authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        raw = authorization.removeprefix("Bearer ").strip()
        if not self.repo.is_token_valid(hash_token(raw)):
            raise HTTPException(status_code=403, detail="Invalid or revoked token")
        return raw

    def revoke(self, token_id: str, now_ms) -> bool:
        return self.repo.revoke_token(token_id, now_ms)


def auth_dependency(token_service: TokenService):
    def verify(authorization: str | None = Header(default=None)) -> str:
        return token_service.verify(authorization)
    return verify
