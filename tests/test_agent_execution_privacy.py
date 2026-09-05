from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from hassan_cloud.agent_execution_privacy import build_hash_only_audit, purge_private_artifact
from hassan_cloud.storage.files import FileStore


class Repo:
    def __init__(self, path: Path):
        self.path = str(path)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def test_hash_only_audit_drops_goal_file_bytes_signature_and_nonce():
    request = {
        "device_id": "phone-1",
        "permit_id": "permit-1",
        "execution_request_id": "exec-1",
        "approval_nonce": "TOP_SECRET_NONCE",
        "project_id": "p1",
        "agent_id": "agent-1",
        "version": "1.2.3",
        "task_id": "task-1",
        "goal": "TOP_SECRET_GOAL",
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
        "args": ["--acp"],
        "protocol_version": 1,
        "actions": ["READ_FILES"],
        "device_signature_base64": "TOP_SECRET_SIGNATURE",
        "files": [
            {"path": "src/Main.kt", "sha256": "c" * 64, "content_base64": "TOP_SECRET_CONTENT"},
        ],
    }
    audit = build_hash_only_audit(request, "d" * 64)
    text = audit.decode("utf-8")
    assert "TOP_SECRET_GOAL" not in text
    assert "TOP_SECRET_CONTENT" not in text
    assert "TOP_SECRET_SIGNATURE" not in text
    assert "TOP_SECRET_NONCE" not in text
    assert '"goal_sha256":"' + "a" * 64 + '"' in text
    assert '"sha256":"' + "c" * 64 + '"' in text
    assert '"raw_goal_retained":false' in text
    assert '"raw_file_bytes_retained":false' in text


def test_local_raw_artifact_bytes_and_metadata_are_deleted(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    store = FileStore(root=root)
    storage_path, digest, size = store.save("artifact-1", "request.json", b"private bytes")
    repo = Repo(tmp_path / "db.sqlite")
    with repo.connection() as conn:
        conn.execute(
            "CREATE TABLE artifacts (id TEXT PRIMARY KEY, storage_path TEXT, sha256 TEXT, size_bytes INTEGER)"
        )
        conn.execute(
            "INSERT INTO artifacts (id,storage_path,sha256,size_bytes) VALUES (?,?,?,?)",
            ("artifact-1", storage_path, digest, size),
        )
    artifact = {"id": "artifact-1", "storage_path": storage_path}
    assert store.absolute(storage_path).exists()
    purge_private_artifact(repo, store, artifact)
    assert not store.absolute(storage_path).exists()
    with repo.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM artifacts WHERE id=?", ("artifact-1",)).fetchone()[0]
    assert count == 0


def test_purge_rejects_local_path_escape(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    store = FileStore(root=root)
    repo = Repo(tmp_path / "db.sqlite")
    with repo.connection() as conn:
        conn.execute("CREATE TABLE artifacts (id TEXT PRIMARY KEY, storage_path TEXT)")
        conn.execute("INSERT INTO artifacts VALUES (?,?)", ("artifact-1", "../outside"))
    try:
        purge_private_artifact(repo, store, {"id": "artifact-1", "storage_path": "../outside"})
        raise AssertionError("path escape should fail")
    except ValueError as exc:
        assert "escapes" in str(exc)
