"""Privacy cleanup for completed one-time Agent tasks.

After execution evidence is durably stored, the raw request artifact (goal + file bytes + device
signature) is replaced by a hash-only audit. This module works with both local FileStore/SQLite and
DbBlobStore/Postgres without exposing a generic deletion API to public routes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HASH_ONLY_AUDIT_ARTIFACT = "agent-acp-task-request-audit.json"


def build_hash_only_audit(request: dict[str, Any], raw_request_sha256: str) -> bytes:
    files = []
    for item in request.get("files") or []:
        if not isinstance(item, dict):
            continue
        files.append({
            "path": str(item.get("path") or ""),
            "sha256": str(item.get("sha256") or "").lower(),
        })
    payload = {
        "schema_version": 1,
        "policy": "frishta-agent-request-privacy-audit-v1",
        "raw_request_sha256": raw_request_sha256.lower(),
        "device_id": str(request.get("device_id") or ""),
        "permit_id": str(request.get("permit_id") or ""),
        "execution_request_id": str(request.get("execution_request_id") or ""),
        "project_id": str(request.get("project_id") or ""),
        "agent_id": str(request.get("agent_id") or ""),
        "version": str(request.get("version") or ""),
        "task_id": str(request.get("task_id") or ""),
        "goal_sha256": str(request.get("goal_sha256") or "").lower(),
        "approval_evidence_id": str(request.get("approval_evidence_id") or ""),
        "comparison_evidence_id": str(request.get("comparison_evidence_id") or ""),
        "static_evidence_id": str(request.get("static_evidence_id") or ""),
        "security_verification_job_id": str(request.get("security_verification_job_id") or ""),
        "benchmark_job_id": str(request.get("benchmark_job_id") or ""),
        "shadow_job_id": str(request.get("shadow_job_id") or ""),
        "source_url": str(request.get("source_url") or ""),
        "expected_sha256": str(request.get("expected_sha256") or "").lower(),
        "command": str(request.get("command") or ""),
        "args": list(request.get("args") or []),
        "protocol_version": int(request.get("protocol_version") or 0),
        "actions": sorted(set(str(value) for value in (request.get("actions") or []))),
        "files": sorted(files, key=lambda value: value["path"]),
        "raw_goal_retained": False,
        "raw_file_bytes_retained": False,
        "device_signature_retained": False,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def purge_private_artifact(repo: Any, files: Any, artifact: dict[str, Any]) -> None:
    """Delete raw bytes first (privacy-first), then artifact metadata. Raises on unsafe local paths."""
    artifact_id = str(artifact.get("id") or "")
    storage_path = str(artifact.get("storage_path") or "")
    if not artifact_id or not storage_path:
        raise ValueError("artifact id/storage path missing")
    postgres = repo.__class__.__name__ == "PostgresRepository"
    placeholder = "%s" if postgres else "?"

    if storage_path.startswith("db://"):
        blob_id = storage_path.removeprefix("db://")
        if blob_id != artifact_id:
            raise ValueError("db artifact storage binding mismatch")
        with repo.connection() as conn:
            conn.execute(f"DELETE FROM artifact_blobs WHERE artifact_id={placeholder}", (artifact_id,))
            conn.execute(f"DELETE FROM artifacts WHERE id={placeholder}", (artifact_id,))
        return

    root_value = getattr(files, "root", None)
    if root_value is None:
        raise ValueError("local file store root unavailable")
    root = Path(root_value).resolve()
    target = (root / storage_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact path escapes file store") from exc
    if target.exists():
        if not target.is_file():
            raise ValueError("artifact storage target is not a file")
        target.unlink()
    with repo.connection() as conn:
        conn.execute(f"DELETE FROM artifacts WHERE id={placeholder}", (artifact_id,))
    parent = target.parent
    if parent != root:
        try:
            parent.rmdir()
        except OSError:
            pass


def raw_request_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
