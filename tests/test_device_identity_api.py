from __future__ import annotations

import base64
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

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def test_enrollment_requires_proof_of_possession_and_is_idempotent(tmp_path):
    private = ec.generate_private_key(ec.SECP256R1())
    der = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    public_b64 = base64.b64encode(der).decode("ascii")
    fingerprint = public_key_fingerprint(public_b64)
    timestamp = 1000
    payload = canonical_enrollment("phone-1", fingerprint, timestamp)
    signature = base64.b64encode(private.sign(payload.encode(), ec.ECDSA(hashes.SHA256()))).decode("ascii")

    app = FastAPI()
    app.include_router(build_device_identity_router(repo=Repo(str(tmp_path / "db.sqlite")), verify_token=lambda: "ok", now_ms=lambda: timestamp))
    client = TestClient(app)
    body = {
        "device_id": "phone-1",
        "public_key_base64": public_b64,
        "timestamp_ms": timestamp,
        "signature_base64": signature,
    }
    first = client.post("/v1/device-identities/enroll", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "ENROLLED"
    second = client.post("/v1/device-identities/enroll", json=body)
    assert second.status_code == 200
    assert second.json()["status"] == "EXISTING"

    bad = dict(body)
    bad["signature_base64"] = base64.b64encode(b"not-a-signature").decode("ascii")
    rejected = client.post("/v1/device-identities/enroll", json=bad)
    assert rejected.status_code == 400
