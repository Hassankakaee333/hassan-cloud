"""PostgreSQL repository — production / Leapcell."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..config import DATABASE_URL, ensure_dirs
from .schema_sql import POSTGRES_SCHEMA

DEFAULT_LEASE_MS = 120_000


class PostgresRepository:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or DATABASE_URL
        if not self._dsn:
            raise ValueError("DATABASE_URL is required for PostgresRepository")
        self._lock = threading.Lock()
        ensure_dirs()

    @contextmanager
    def connection(self):
        with self._lock:
            with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute(POSTGRES_SCHEMA)
            self._ensure_job_columns(conn)

    def _ensure_job_columns(self, conn) -> None:
        cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='jobs'"
        ).fetchall()}
        alters = {
            "idempotency_key": "TEXT",
            "lease_expires_at": "BIGINT",
            "worker_id": "TEXT",
            "checkpoint_stage": "TEXT NOT NULL DEFAULT ''",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        }
        for col, typedef in alters.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typedef}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key) "
            "WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''"
        )
        agent_cols = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='agent_runs'"
        ).fetchall()}
        for col in ("started_at", "finished_at"):
            if col not in agent_cols:
                conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {col} BIGINT")

    def health_check(self) -> bool:
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def save_artifact_blob(self, artifact_id: str, data: bytes) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO artifact_blobs (artifact_id, data) VALUES (%s, %s) "
                "ON CONFLICT (artifact_id) DO UPDATE SET data = EXCLUDED.data",
                (artifact_id, data),
            )

    def read_artifact_blob(self, artifact_id: str) -> bytes | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT data FROM artifact_blobs WHERE artifact_id=%s", (artifact_id,),
            ).fetchone()
            return row["data"] if row else None

    # --- tokens ---
    def insert_token(self, token_id: str, token_hash: str, label: str, device_id: str | None, created_at: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO api_tokens (id, token_hash, label, device_id, created_at, revoked_at) "
                "VALUES (%s,%s,%s,%s,%s,NULL)",
                (token_id, token_hash, label, device_id, created_at),
            )

    def is_token_valid(self, token_hash: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id FROM api_tokens WHERE token_hash=%s AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
            return row is not None

    def revoke_token(self, token_id: str, revoked_at: int) -> bool:
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE api_tokens SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL",
                (revoked_at, token_id),
            )
            return cur.rowcount > 0

    def list_tokens(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, label, device_id, created_at, revoked_at FROM api_tokens ORDER BY created_at DESC",
            ).fetchall()
            return [dict(r) for r in rows]

    # --- projects ---
    def create_project(self, pid: str, name: str, description: str, workspace_path: str | None, ts: int) -> dict:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO projects (id,name,description,workspace_path,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
                (pid, name, description, workspace_path, ts, ts),
            )
            return dict(conn.execute("SELECT * FROM projects WHERE id=%s", (pid,)).fetchone())

    def get_project(self, pid: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=%s", (pid,)).fetchone()
            return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        with self.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()]

    def project_workspace(self, project_id: str) -> dict:
        with self.connection() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=%s", (project_id,)).fetchone()
            if not project:
                return {}
            jobs = conn.execute("SELECT * FROM jobs WHERE project_id=%s ORDER BY updated_at DESC", (project_id,)).fetchall()
            artifacts = conn.execute(
                "SELECT * FROM artifacts WHERE project_id=%s ORDER BY created_at DESC", (project_id,),
            ).fetchall()
            convs = conn.execute(
                "SELECT * FROM conversations WHERE project_id=%s ORDER BY updated_at DESC", (project_id,),
            ).fetchall()
            return {
                "project": dict(project),
                "conversations": [dict(r) for r in convs],
                "jobs": [dict(r) for r in jobs],
                "artifacts": [dict(r) for r in artifacts],
            }

    # --- jobs ---
    def create_job(
        self, jid: str, project_id: str, conversation_id: str | None, goal: str,
        job_type: str, ts: int, idempotency_key: str | None = None,
    ) -> dict:
        with self.connection() as conn:
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key=%s", (idempotency_key,),
                ).fetchone()
                if row:
                    return dict(row)
            conn.execute(
                "INSERT INTO jobs (id,project_id,conversation_id,goal,job_type,state,result_summary,log,"
                "created_at,updated_at,idempotency_key,lease_expires_at,worker_id,checkpoint_stage,cancel_requested) "
                "VALUES (%s,%s,%s,%s,%s,'QUEUED',NULL,'',%s,%s,%s,NULL,NULL,'',0)",
                (jid, project_id, conversation_id, goal, job_type, ts, ts, idempotency_key),
            )
            return dict(conn.execute("SELECT * FROM jobs WHERE id=%s", (jid,)).fetchone())

    def claim_job_by_id(self, job_id: str, worker_id: str, now_ms: int, lease_ms: int = DEFAULT_LEASE_MS) -> dict | None:
        with self.connection() as conn:
            lease_until = now_ms + lease_ms
            cur = conn.execute(
                "UPDATE jobs SET state='RUNNING', worker_id=%s, lease_expires_at=%s, updated_at=%s "
                "WHERE id=%s AND state='QUEUED' AND cancel_requested=0",
                (worker_id, lease_until, now_ms, job_id),
            )
            if cur.rowcount == 0:
                row = conn.execute("SELECT * FROM jobs WHERE id=%s AND state='RUNNING'", (job_id,)).fetchone()
                return dict(row) if row else None
            return dict(conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone())

    def recover_stale_jobs(self, now_ms: int) -> int:
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET state='QUEUED', worker_id=NULL, lease_expires_at=NULL, "
                "log=log || '[recovery] stale lease — re-queued\n', updated_at=%s "
                "WHERE state='RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at < %s "
                "AND cancel_requested=0",
                (now_ms, now_ms),
            )
            return cur.rowcount

    def claim_next_queued_job(self, worker_id: str, now_ms: int, lease_ms: int = DEFAULT_LEASE_MS) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE state='QUEUED' AND cancel_requested=0 ORDER BY created_at ASC LIMIT 1",
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE state='RUNNING' AND lease_expires_at IS NOT NULL "
                    "AND lease_expires_at < %s AND cancel_requested=0 ORDER BY updated_at ASC LIMIT 1",
                    (now_ms,),
                ).fetchone()
                if not row:
                    return None
            lease_until = now_ms + lease_ms
            cur = conn.execute(
                "UPDATE jobs SET state='RUNNING', worker_id=%s, lease_expires_at=%s, updated_at=%s "
                "WHERE id=%s AND cancel_requested=0 AND (state='QUEUED' OR "
                "(state='RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at < %s))",
                (worker_id, lease_until, now_ms, row["id"], now_ms),
            )
            if cur.rowcount == 0:
                return None
            return dict(conn.execute("SELECT * FROM jobs WHERE id=%s", (row["id"],)).fetchone())

    def renew_lease(self, job_id: str, worker_id: str, now_ms: int, lease_ms: int = DEFAULT_LEASE_MS) -> bool:
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET lease_expires_at=%s, updated_at=%s WHERE id=%s AND worker_id=%s AND state='RUNNING'",
                (now_ms + lease_ms, now_ms, job_id, worker_id),
            )
            return cur.rowcount > 0

    def set_checkpoint(self, job_id: str, stage: str, ts: int) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE jobs SET checkpoint_stage=%s, updated_at=%s WHERE id=%s", (stage, ts, job_id))

    def is_job_cancelled(self, job_id: str) -> bool:
        with self.connection() as conn:
            row = conn.execute("SELECT cancel_requested, state FROM jobs WHERE id=%s", (job_id,)).fetchone()
            if not row:
                return False
            return bool(row["cancel_requested"]) or row["state"] == "CANCELLED"

    def update_job(self, job_id: str, state: str, log_append: str, summary: str | None, ts: int) -> None:
        with self.connection() as conn:
            row = conn.execute("SELECT log FROM jobs WHERE id=%s", (job_id,)).fetchone()
            log = (row["log"] or "") + log_append
            conn.execute(
                "UPDATE jobs SET state=%s, log=%s, result_summary=COALESCE(%s, result_summary), updated_at=%s WHERE id=%s",
                (state, log, summary, ts, job_id),
            )

    def get_job(self, job_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT %s", (limit,),
            ).fetchall()]

    def cancel_job(self, job_id: str, ts: int) -> bool:
        with self.connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET cancel_requested=1, "
                "state=CASE WHEN state IN ('QUEUED','WAITING_FOR_USER') THEN 'CANCELLED' ELSE state END, "
                "log=log || '[cancel] requested by client\n', updated_at=%s "
                "WHERE id=%s AND state NOT IN ('COMPLETED','FAILED','CANCELLED')",
                (ts, job_id),
            )
            return cur.rowcount > 0

    def finalize_cancelled(self, job_id: str, ts: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE jobs SET state='CANCELLED', updated_at=%s WHERE id=%s AND cancel_requested=1",
                (ts, job_id),
            )

    # --- artifacts ---
    def create_artifact(self, row: dict) -> dict:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO artifacts (id,project_id,job_id,conversation_id,name,mime_type,size_bytes,storage_path,sha256,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    row["id"], row.get("project_id"), row.get("job_id"), row.get("conversation_id"),
                    row["name"], row["mime_type"], row["size_bytes"], row.get("storage_path"),
                    row.get("sha256"), row["created_at"],
                ),
            )
            return dict(conn.execute("SELECT * FROM artifacts WHERE id=%s", (row["id"],)).fetchone())

    def get_artifact(self, artifact_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id=%s", (artifact_id,)).fetchone()
            return dict(row) if row else None

    def list_artifacts(self, project_id: str | None = None, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM artifacts WHERE project_id=%s ORDER BY created_at DESC LIMIT %s",
                    (project_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT %s", (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    # --- agent runs ---
    def insert_agent_run(self, row: dict) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO agent_runs (id,job_id,agent_id,agent_role,status,input_text,output_text,verification_notes,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    row["id"], row["job_id"], row["agent_id"], row["agent_role"], row["status"],
                    row["input_text"], row["output_text"], row.get("verification_notes"), row["created_at"],
                ),
            )

    def list_agent_runs(self, job_id: str) -> list[dict]:
        with self.connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM agent_runs WHERE job_id=%s ORDER BY created_at", (job_id,),
            ).fetchall()]

    def has_completed_agent(self, job_id: str, agent_role: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id FROM agent_runs WHERE job_id=%s AND agent_role=%s AND status='COMPLETED' LIMIT 1",
                (job_id, agent_role),
            ).fetchone()
            return row is not None

    # --- radar ---
    def upsert_radar_candidate(self, row: dict) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO radar_candidates "
                "(id,name,candidate_type,source,url,license,cost_type,capabilities,status,discovered_at,last_evaluated_at,notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "name=EXCLUDED.name, candidate_type=EXCLUDED.candidate_type, source=EXCLUDED.source, "
                "url=EXCLUDED.url, license=EXCLUDED.license, cost_type=EXCLUDED.cost_type, "
                "capabilities=EXCLUDED.capabilities, status=EXCLUDED.status, "
                "discovered_at=EXCLUDED.discovered_at, last_evaluated_at=EXCLUDED.last_evaluated_at, notes=EXCLUDED.notes",
                (
                    row["id"], row["name"], row["candidate_type"], row["source"], row["url"],
                    row.get("license"), row["cost_type"], json.dumps(row.get("capabilities", [])),
                    row["status"], row["discovered_at"], row.get("last_evaluated_at"), row.get("notes"),
                ),
            )

    def list_radar_candidates(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM radar_candidates ORDER BY discovered_at DESC").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["capabilities"] = json.loads(d.get("capabilities") or "[]")
                out.append(d)
            return out
