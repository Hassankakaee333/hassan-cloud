from __future__ import annotations

import base64
import hashlib
import sqlite3
from contextlib import contextmanager

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hassan_cloud.device_identity import canonical_enrollment, public_key_fingerprint
from hassan_cloud.device_identity_api import build_device_identity_router


class Repo:
    def __init__(self, path: str):
        self.path = path
        with self.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS api_tokens ("
                "id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL, "
                "device_id TEXT, created_at INTEGER NOT NULL, revoked_at INTEGER)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO api_tokens "
                "(id,token_hash,label,device_id,created_at,revoked_at) VALUES (?,?,?,?,?,NULL)",
                ("token-1", hashlib.sha256(b"ok").hexdigest(), "device", None, 1),
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


def signed_enrollment(device_id: str, timestamp: int):
    private = ec.generate_private_key(ec.SECP256R1())
    der = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_b64 = base64.b64encode(der).decode("ascii")
    fingerprint = public_key_fingerprint(public_b64)
    payload = canonical_enrollment(device_id, fingerprint, timestamp)
    signature = base64.b64encode(
        private.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))
    ).decode("ascii")
    return {
        "device_id": device_id,
        "public_key_base64": public_b64,
        "timestamp_ms": timestamp,
        "signature_base64": signature,
    }, fingerprint


def test_enrollment_requires_proof_of_possession_binds_bearer_and_is_idempotent(tmp_path):
    timestamp = 1000
    repo = Repo(str(tmp_path / "db.sqlite"))
    body, fingerprint = signed_enrollment("phone-1", timestamp)

    app = FastAPI()
    app.include_router(
        build_device_identity_router(
            repo=repo,
            verify_token=lambda: "ok",
            now_ms=lambda: timestamp,
        )
    )
    client = TestClient(app)

    first = client.post("/v1/device-identities/enroll", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "ENROLLED"
    assert first.json()["fingerprint_sha256"] == fingerprint
    assert first.json()["bearer_device_binding"] == "BOUND"

    second = client.post("/v1/device-identities/enroll", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "EXISTING"
    assert second.json()["bearer_device_binding"] == "EXISTING"

    with repo.connection() as conn:
        row = conn.execute("SELECT device_id FROM api_tokens WHERE id='token-1'").fetchone()
        assert row["device_id"] == "phone-1"

    bad = dict(body)
    bad["signature_base64"] = base64.b64encode(b"not-a-signature").decode("ascii")
    rejected = client.post("/v1/device-identities/enroll", json=bad)
    assert rejected.status_code == 400


def test_bound_bearer_cannot_enroll_a_different_device(tmp_path):
    timestamp = 1000
    repo = Repo(str(tmp_path / "db.sqlite"))
    first_body, _ = signed_enrollment("phone-1", timestamp)
    other_body, _ = signed_enrollment("phone-2", timestamp)

    app = FastAPI()
    app.include_router(
        build_device_identity_router(
            repo=repo,
            verify_token=lambda: "ok",
            now_ms=lambda: timestamp,
        )
    )
    client = TestClient(app)

    first = client.post("/v1/device-identities/enroll", json=first_body)
    assert first.status_code == 200, first.text

    other = client.post("/v1/device-identities/enroll", json=other_body)
    assert other.status_code == 403
    assert "bound to another device" in other.text
