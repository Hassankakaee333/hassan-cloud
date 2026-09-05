"""One-time claim gate for private P2.11 Agent execution bundles.

A trusted prepare job may consume a Cloud request bundle exactly once. The raw request is then
replaced by the existing hash-only privacy audit before the response is returned. Any later prepare
retry must fail closed; a spent phone permit is never reopened just because delivery failed.
"""
from __future__ import annotations

from typing import Any


class AgentExecutionBundleClaimStore:
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
                "CREATE TABLE IF NOT EXISTS agent_execution_bundle_claims ("
                "job_id TEXT PRIMARY KEY, claimed_at BIGINT NOT NULL)"
            )

    def claim_once(self, job_id: str, claimed_at: int) -> bool:
        if not job_id or len(job_id) > 128:
            raise ValueError("invalid Agent execution job id")
        if claimed_at <= 0:
            raise ValueError("invalid bundle claim timestamp")
        with self.repo.connection() as conn:
            if self.postgres:
                cur = conn.execute(
                    "INSERT INTO agent_execution_bundle_claims (job_id,claimed_at) VALUES (%s,%s) "
                    "ON CONFLICT (job_id) DO NOTHING",
                    (job_id, claimed_at),
                )
            else:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO agent_execution_bundle_claims (job_id,claimed_at) VALUES (?,?)",
                    (job_id, claimed_at),
                )
            return cur.rowcount == 1

    def is_claimed(self, job_id: str) -> bool:
        with self.repo.connection() as conn:
            row = conn.execute(
                f"SELECT job_id FROM agent_execution_bundle_claims WHERE job_id={self.p}",
                (job_id,),
            ).fetchone()
            return row is not None
