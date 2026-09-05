"""Pinned Hassan device identity for P2.9 execution authorization.

The private key never leaves Android Keystore. Cloud stores only the DER public key and its SHA-256
fingerprint. First enrollment requires API authentication plus proof-of-possession. Once pinned,
replacement requires signatures from both the currently pinned key and the new key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

MAX_CLOCK_SKEW_MS = 10 * 60 * 1000


def _field(name: str, value: str) -> str:
    return f"{name}:{len(value.encode('utf-8'))}:{value}\n"


def _fingerprint(public_key_der: bytes) -> str:
    return hashlib.sha256(public_key_der).hexdigest()


def _decode_public_key(value: str) -> tuple[ec.EllipticCurvePublicKey, bytes, str]:
    try:
        der = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("invalid device public key base64") from exc
    if len(der) < 32 or len(der) > 4096:
        raise ValueError("invalid device public key size")
    try:
        key = serialization.load_der_public_key(der)
    except Exception as exc:
        raise ValueError("invalid device public key DER") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("device key must be EC P-256")
    return key, der, _fingerprint(der)


def verify_signature(public_key_b64: str, payload: str, signature_b64: str) -> None:
    key, _der, _fingerprint_value = _decode_public_key(public_key_b64)
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise ValueError("invalid device signature base64") from exc
    if not signature or len(signature) > 256:
        raise ValueError("invalid device signature size")
    try:
        key.verify(signature, payload.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ValueError("device signature verification failed") from exc


def canonical_enrollment(device_id: str, public_key_fingerprint: str, timestamp_ms: int) -> str:
    return "".join((
        _field("policy", "frishta-device-enrollment-v1"),
        _field("device_id", device_id),
        _field("public_key_sha256", public_key_fingerprint),
        _field("timestamp_ms", str(timestamp_ms)),
    ))


def canonical_rotation(
    device_id: str,
    prior_fingerprint: str,
    new_fingerprint: str,
    timestamp_ms: int,
) -> str:
    return "".join((
        _field("policy", "frishta-device-rotation-v1"),
        _field("device_id", device_id),
        _field("prior_public_key_sha256", prior_fingerprint),
        _field("new_public_key_sha256", new_fingerprint),
        _field("timestamp_ms", str(timestamp_ms)),
    ))


def assert_fresh_timestamp(timestamp_ms: int, now_ms: int | None = None) -> None:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if timestamp_ms <= 0 or abs(now - timestamp_ms) > MAX_CLOCK_SKEW_MS:
        raise ValueError("device identity timestamp outside allowed window")


@dataclass(frozen=True)
class DeviceIdentityRecord:
    device_id: str
    public_key_base64: str
    fingerprint_sha256: str
    created_at: int
    updated_at: int


class DeviceIdentityStore:
    """Small dialect-neutral store layered over the existing repository connection abstraction."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.postgres = repo.__class__.__name__ == "PostgresRepository"
        self.ensure_schema()

    @property
    def p(self) -> str:
        return "%s" if self.postgres else "?"

    def ensure_schema(self) -> None:
        with self.repo.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS device_identities ("
                "device_id TEXT PRIMARY KEY, public_key_base64 TEXT NOT NULL, "
                "fingerprint_sha256 TEXT NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)"
            )

    def get(self, device_id: str) -> DeviceIdentityRecord | None:
        with self.repo.connection() as conn:
            row = conn.execute(
                f"SELECT device_id,public_key_base64,fingerprint_sha256,created_at,updated_at "
                f"FROM device_identities WHERE device_id={self.p}",
                (device_id,),
            ).fetchone()
            return DeviceIdentityRecord(**dict(row)) if row else None

    def create_once(self, record: DeviceIdentityRecord) -> bool:
        with self.repo.connection() as conn:
            existing = conn.execute(
                f"SELECT device_id FROM device_identities WHERE device_id={self.p}",
                (record.device_id,),
            ).fetchone()
            if existing:
                return False
            placeholders = ",".join([self.p] * 5)
            conn.execute(
                f"INSERT INTO device_identities "
                f"(device_id,public_key_base64,fingerprint_sha256,created_at,updated_at) VALUES ({placeholders})",
                (
                    record.device_id,
                    record.public_key_base64,
                    record.fingerprint_sha256,
                    record.created_at,
                    record.updated_at,
                ),
            )
            return True

    def rotate(
        self,
        device_id: str,
        prior_fingerprint: str,
        new_public_key_base64: str,
        new_fingerprint: str,
        updated_at: int,
    ) -> bool:
        with self.repo.connection() as conn:
            cur = conn.execute(
                f"UPDATE device_identities SET public_key_base64={self.p}, fingerprint_sha256={self.p}, "
                f"updated_at={self.p} WHERE device_id={self.p} AND fingerprint_sha256={self.p}",
                (new_public_key_base64, new_fingerprint, updated_at, device_id, prior_fingerprint),
            )
            return cur.rowcount == 1


def public_key_fingerprint(public_key_base64: str) -> str:
    _key, _der, fingerprint = _decode_public_key(public_key_base64)
    return fingerprint


def canonical_execution_attestation(payload: dict[str, Any]) -> str:
    """Canonical P2.9 device-signed execution scope; file contents are represented only by SHA-256."""
    fields: list[tuple[str, str]] = [
        ("policy", "frishta-agent-execution-attestation-v1"),
        ("device_id", str(payload.get("device_id") or "")),
        ("permit_id", str(payload.get("permit_id") or "")),
        ("execution_request_id", str(payload.get("execution_request_id") or "")),
        ("project_id", str(payload.get("project_id") or "")),
        ("agent_id", str(payload.get("agent_id") or "")),
        ("version", str(payload.get("version") or "")),
        ("task_id", str(payload.get("task_id") or "")),
        ("goal_sha256", str(payload.get("goal_sha256") or "").lower()),
        ("approval_evidence_id", str(payload.get("approval_evidence_id") or "")),
        ("comparison_evidence_id", str(payload.get("comparison_evidence_id") or "")),
        ("static_evidence_id", str(payload.get("static_evidence_id") or "")),
        ("security_job_id", str(payload.get("security_verification_job_id") or "")),
        ("benchmark_job_id", str(payload.get("benchmark_job_id") or "")),
        ("shadow_job_id", str(payload.get("shadow_job_id") or "")),
        ("source_url", str(payload.get("source_url") or "")),
        ("expected_sha256", str(payload.get("expected_sha256") or "").lower()),
        ("command", str(payload.get("command") or "")),
        ("protocol_version", str(payload.get("protocol_version") or "")),
    ]
    canonical = "".join(_field(name, value) for name, value in fields)
    raw_files = payload.get("files") or []
    normalized_files = sorted(
        (str(item.get("path") or ""), str(item.get("sha256") or "").lower())
        for item in raw_files if isinstance(item, dict)
    )
    for path, digest in normalized_files:
        canonical += _field("file", path) + _field("file_sha256", digest)
    for action in sorted(set(str(item) for item in (payload.get("actions") or []))):
        canonical += _field("action", action)
    for arg in payload.get("args") or []:
        canonical += _field("arg", str(arg))
    return canonical


def verify_pinned_execution_signature(
    store: DeviceIdentityStore,
    device_id: str,
    payload: dict[str, Any],
    signature_base64: str,
) -> DeviceIdentityRecord:
    record = store.get(device_id)
    if record is None:
        raise ValueError("device identity is not enrolled")
    canonical = canonical_execution_attestation(payload)
    verify_signature(record.public_key_base64, canonical, signature_base64)
    return record
