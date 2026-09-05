from __future__ import annotations

import base64
import sqlite3
from contextlib import contextmanager

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hassan_cloud.device_identity import (
    DeviceIdentityRecord,
    DeviceIdentityStore,
    canonical_enrollment,
    canonical_execution_attestation,
    canonical_rotation,
    public_key_fingerprint,
    verify_pinned_execution_signature,
    verify_signature,
)


class SqliteRepo:
    def __init__(self, path: str) -> None:
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


def key_material():
    private = ec.generate_private_key(ec.SECP256R1())
    public_der = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_b64 = base64.b64encode(public_der).decode("ascii")
    return private, public_b64


def sign(private, payload: str) -> str:
    raw = private.sign(payload.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(raw).decode("ascii")


def test_enrollment_and_execution_signature_are_bound_to_pinned_key(tmp_path):
    private, public_b64 = key_material()
    fingerprint = public_key_fingerprint(public_b64)
    enrollment = canonical_enrollment("phone-1", fingerprint, 123)
    verify_signature(public_b64, enrollment, sign(private, enrollment))

    store = DeviceIdentityStore(SqliteRepo(str(tmp_path / "identity.db")))
    assert store.create_once(DeviceIdentityRecord("phone-1", public_b64, fingerprint, 1, 1))
    assert not store.create_once(DeviceIdentityRecord("phone-1", public_b64, fingerprint, 2, 2))

    execution = {
        "device_id": "phone-1",
        "permit_id": "permit-1",
        "execution_request_id": "exec-1",
        "project_id": "p1",
        "agent_id": "agent",
        "version": "1.2.3",
        "task_id": "task-1",
        "goal_sha256": "a" * 64,
        "approval_evidence_id": "approval-1",
        "comparison_evidence_id": "compare-1",
        "static_evidence_id": "static-1",
        "security_verification_job_id": "security-1",
        "benchmark_job_id": "benchmark-1",
        "shadow_job_id": "shadow-1",
        "source_url": "https://example.com/agent.tar.gz",
        "expected_sha256": "b" * 64,
        "command": "bin/agent",
        "protocol_version": 1,
        "files": [{"path": "docs/a.txt", "sha256": "c" * 64}],
        "actions": ["READ_FILES"],
        "args": ["--acp"],
    }
    payload = canonical_execution_attestation(execution)
    signature = sign(private, payload)
    record = verify_pinned_execution_signature(store, "phone-1", execution, signature)
    assert record.fingerprint_sha256 == fingerprint

    changed = dict(execution)
    changed["goal_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="signature verification failed"):
        verify_pinned_execution_signature(store, "phone-1", changed, signature)


def test_rotation_payload_requires_old_and_new_key_possession():
    old_private, old_public = key_material()
    new_private, new_public = key_material()
    old_fp = public_key_fingerprint(old_public)
    new_fp = public_key_fingerprint(new_public)
    payload = canonical_rotation("phone-1", old_fp, new_fp, 456)
    verify_signature(old_public, payload, sign(old_private, payload))
    verify_signature(new_public, payload, sign(new_private, payload))
    with pytest.raises(ValueError):
        verify_signature(new_public, payload, sign(old_private, payload))
