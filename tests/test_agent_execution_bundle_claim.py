from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from hassan_cloud.agent_execution_bundle_claim import AgentExecutionBundleClaimStore


class SqliteRepo:
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


def test_bundle_claim_is_atomic_and_irreversible(tmp_path):
    store = AgentExecutionBundleClaimStore(SqliteRepo(tmp_path / "claims.sqlite"))
    assert store.claim_once("job-1234567890123456", 1000) is True
    assert store.is_claimed("job-1234567890123456") is True
    assert store.claim_once("job-1234567890123456", 2000) is False


def test_bundle_claim_rejects_invalid_identity(tmp_path):
    store = AgentExecutionBundleClaimStore(SqliteRepo(tmp_path / "claims.sqlite"))
    try:
        store.claim_once("", 1000)
        raise AssertionError("empty job id should fail")
    except ValueError as exc:
        assert "job id" in str(exc)
    try:
        store.claim_once("job-1234567890123456", 0)
        raise AssertionError("zero timestamp should fail")
    except ValueError as exc:
        assert "timestamp" in str(exc)
