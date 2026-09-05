from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager

from hassan_cloud.device_token_binding import DeviceTokenBindingStore


class Repo:
    def __init__(self, path: str):
        self.path = path
        with self.connection() as conn:
            conn.execute(
                "CREATE TABLE api_tokens ("
                "id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL, "
                "device_id TEXT, created_at INTEGER NOT NULL, revoked_at INTEGER)"
            )

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_token(self, raw: str, device_id=None, revoked_at=None):
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO api_tokens (id,token_hash,label,device_id,created_at,revoked_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    f"token-{raw}", hashlib.sha256(raw.encode()).hexdigest(), "device",
                    device_id, 1, revoked_at,
                ),
            )


def test_unbound_active_token_binds_once_to_device(tmp_path):
    repo = Repo(str(tmp_path / "db.sqlite"))
    repo.add_token("secret")
    store = DeviceTokenBindingStore(repo)

    assert store.bind_or_verify("secret", "phone-1") == "BOUND"
    assert store.bind_or_verify("secret", "phone-1") == "EXISTING"
    store.require_bound("secret", "phone-1")

    try:
        store.require_bound("secret", "phone-2")
        raise AssertionError("different device should fail")
    except ValueError as exc:
        assert "not bound to this device" in str(exc)


def test_bound_token_cannot_move_to_another_device(tmp_path):
    repo = Repo(str(tmp_path / "db.sqlite"))
    repo.add_token("secret", device_id="phone-1")
    store = DeviceTokenBindingStore(repo)

    try:
        store.bind_or_verify("secret", "phone-2")
        raise AssertionError("device binding must be immutable")
    except ValueError as exc:
        assert "bound to another device" in str(exc)


def test_revoked_or_unknown_token_fails_closed(tmp_path):
    repo = Repo(str(tmp_path / "db.sqlite"))
    repo.add_token("revoked", revoked_at=10)
    store = DeviceTokenBindingStore(repo)

    for raw in ("revoked", "unknown"):
        try:
            store.bind_or_verify(raw, "phone-1")
            raise AssertionError("inactive bearer should fail")
        except ValueError as exc:
            assert "no longer active" in str(exc)
