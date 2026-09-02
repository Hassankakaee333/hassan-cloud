"""Bearer token auth — secrets stay server-side."""

from __future__ import annotations

import os

from fastapi import Header, HTTPException

DEFAULT_TOKEN = os.environ.get("HASSAN_API_TOKEN", "dev-token-change-me")


def verify_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != DEFAULT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    return token
