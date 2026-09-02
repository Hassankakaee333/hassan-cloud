-- Persistent Hassan project workspace (MVP).
-- Source files remain private in Neon; GitHub Actions receives temporary copies.

CREATE TABLE IF NOT EXISTS workspace_files (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    content_base64 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_workspace_files_project
    ON workspace_files(project_id);
