"""Bind an authenticated Hassan Cloud bearer token to one enrolled Android device.

The existing bearer remains revocable and hashed at rest. After the first proof-of-possession
enrollment, that token may only be used for Agent execution with the same opaque device id.
This hardens post-enrollment token theft; the very first enrollment remains a TOFU boundary unless
an already device-bound token or stronger platform attestation is provisioned beforehand.
"""
from __future__ import annotations

import hashlib
from typing import Any


def _token_hash(raw_token: str) -> str:
    if not raw_token:
        raise ValueError("authenticated bearer token missing")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class DeviceTokenBindingStore:
    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.postgres = repo.__class__.__name__ == "PostgresRepository"

    @property
    def p(self) -> str:
        return "%s" if self.postgres else "?"

    def bind_or_verify(self, raw_token: str, device_id: str) -> str:
        if not device_id or len(device_id) > 128:
            raise ValueError("invalid device id for bearer binding")
        digest = _token_hash(raw_token)
        with self.repo.connection() as conn:
            row = conn.execute(
                f"SELECT id,device_id FROM api_tokens WHERE token_hash={self.p} AND revoked_at IS NULL",
                (digest,),
            ).fetchone()
            if row is None:
                raise ValueError("authenticated bearer token is no longer active")
            existing = row["device_id"]
            if existing:
                if str(existing) != device_id:
                    raise ValueError("bearer token is bound to another device")
                return "EXISTING"
            cur = conn.execute(
                f"UPDATE api_tokens SET device_id={self.p} WHERE token_hash={self.p} "
                f"AND revoked_at IS NULL AND device_id IS NULL",
                (device_id, digest),
            )
            if cur.rowcount == 1:
                return "BOUND"
            reread = conn.execute(
                f"SELECT device_id FROM api_tokens WHERE token_hash={self.p} AND revoked_at IS NULL",
                (digest,),
            ).fetchone()
            if reread is not None and str(reread["device_id"] or "") == device_id:
                return "EXISTING"
            raise ValueError("bearer token device binding changed concurrently")

    def require_bound(self, raw_token: str, device_id: str) -> None:
        if not device_id or len(device_id) > 128:
            raise ValueError("invalid device id for bearer binding")
        digest = _token_hash(raw_token)
        with self.repo.connection() as conn:
            row = conn.execute(
                f"SELECT device_id FROM api_tokens WHERE token_hash={self.p} AND revoked_at IS NULL",
                (digest,),
            ).fetchone()
            if row is None:
                raise ValueError("authenticated bearer token is no longer active")
            if str(row["device_id"] or "") != device_id:
                raise ValueError("bearer token is not bound to this device")
