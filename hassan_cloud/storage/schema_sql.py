"""Shared schema DDL — dialect-specific fragments."""

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    device_id TEXT,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    workspace_path TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    title TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    conversation_id TEXT,
    goal TEXT NOT NULL,
    job_type TEXT NOT NULL DEFAULT 'general',
    state TEXT NOT NULL,
    result_summary TEXT,
    log TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    idempotency_key TEXT,
    lease_expires_at BIGINT,
    worker_id TEXT,
    checkpoint_stage TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key)
    WHERE idempotency_key IS NOT NULL AND idempotency_key != '';
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    job_id TEXT,
    conversation_id TEXT,
    name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    storage_path TEXT,
    sha256 TEXT,
    created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_blobs (
    artifact_id TEXT PRIMARY KEY,
    data BYTEA NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    status TEXT NOT NULL,
    input_text TEXT NOT NULL,
    output_text TEXT NOT NULL,
    verification_notes TEXT,
    created_at BIGINT NOT NULL,
    started_at BIGINT,
    finished_at BIGINT
);
CREATE TABLE IF NOT EXISTS radar_candidates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    candidate_type TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    license TEXT,
    cost_type TEXT NOT NULL,
    capabilities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'NEW',
    discovered_at BIGINT NOT NULL,
    last_evaluated_at BIGINT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    cost_type TEXT NOT NULL,
    health TEXT NOT NULL DEFAULT 'UNKNOWN',
    quality_tier TEXT,
    limits_json TEXT,
    updated_at BIGINT NOT NULL
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    device_id TEXT,
    created_at BIGINT NOT NULL,
    revoked_at BIGINT
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    workspace_path TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    title TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    conversation_id TEXT,
    goal TEXT NOT NULL,
    job_type TEXT NOT NULL DEFAULT 'general',
    state TEXT NOT NULL,
    result_summary TEXT,
    log TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    idempotency_key TEXT,
    lease_expires_at BIGINT,
    worker_id TEXT,
    checkpoint_stage TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key)
    WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    job_id TEXT,
    conversation_id TEXT,
    name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    storage_path TEXT,
    sha256 TEXT,
    created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifact_blobs (
    artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,
    data BYTEA NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    status TEXT NOT NULL,
    input_text TEXT NOT NULL,
    output_text TEXT NOT NULL,
    verification_notes TEXT,
    created_at BIGINT NOT NULL,
    started_at BIGINT,
    finished_at BIGINT
);
CREATE TABLE IF NOT EXISTS radar_candidates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    candidate_type TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    license TEXT,
    cost_type TEXT NOT NULL,
    capabilities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'NEW',
    discovered_at BIGINT NOT NULL,
    last_evaluated_at BIGINT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    cost_type TEXT NOT NULL,
    health TEXT NOT NULL DEFAULT 'UNKNOWN',
    quality_tier TEXT,
    limits_json TEXT,
    updated_at BIGINT NOT NULL
);
"""
