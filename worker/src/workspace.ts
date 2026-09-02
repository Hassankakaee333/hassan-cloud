import { Hono } from "hono";
import { authMiddleware, callbackAuth } from "./auth";
import { nowMs, sql } from "./db";
import type { Env } from "./types";

export const workspaceRoutes = new Hono<{ Bindings: Env }>();

const MAX_FILE_BYTES = 512 * 1024;
const MAX_WORKSPACE_BYTES = 5 * 1024 * 1024;
const MAX_FILES = 300;

function normalizePath(raw: string): string | null {
  const path = raw.trim().replace(/\\/g, "/");
  if (!path || path.length > 240 || path.startsWith("/")) return null;
  const parts = path.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) return null;
  return path;
}

function decodeBase64(value: string): Uint8Array | null {
  try {
    const binary = atob(value);
    return Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
  } catch {
    return null;
  }
}
async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function ensureWorkspaceSchema(env: Env): Promise<void> {
  const db = sql(env);
  await db`CREATE TABLE IF NOT EXISTS workspace_files (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    content_base64 TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (project_id, path)
  )`;
  await db`CREATE INDEX IF NOT EXISTS idx_workspace_files_project
    ON workspace_files(project_id)`;
}

async function projectExists(env: Env, projectId: string): Promise<boolean> {
  const db = sql(env);
  const rows = await db`SELECT id FROM projects WHERE id = ${projectId}`;
  return rows.length > 0;
}
workspaceRoutes.get("/v1/projects/:projectId/files", authMiddleware, async (c) => {
  const projectId = c.req.param("projectId") ?? "";
  await ensureWorkspaceSchema(c.env);
  if (!(await projectExists(c.env, projectId))) {
    return c.json({ detail: "project not found" }, 404);
  }
  const db = sql(c.env);
  const rows = await db`SELECT path, size_bytes, sha256, updated_at
    FROM workspace_files WHERE project_id = ${projectId} ORDER BY path`;
  return c.json(
    rows.map((r) => ({
      path: r.path,
      size_bytes: Number(r.size_bytes),
      sha256: r.sha256,
      updated_at: Number(r.updated_at),
    })),
  );
});

workspaceRoutes.get("/v1/projects/:projectId/file", authMiddleware, async (c) => {
  const projectId = c.req.param("projectId") ?? "";
  const path = normalizePath(c.req.query("path") ?? "");
  if (!path) return c.json({ detail: "invalid path" }, 400);
  await ensureWorkspaceSchema(c.env);
  const db = sql(c.env);
  const rows = await db`SELECT path, content_base64, size_bytes, sha256, updated_at
    FROM workspace_files WHERE project_id = ${projectId} AND path = ${path}`;
  if (rows.length === 0) return c.json({ detail: "file not found" }, 404);
  const r = rows[0];
  return c.json({
    path: r.path,
    content_base64: r.content_base64,
    size_bytes: Number(r.size_bytes),
    sha256: r.sha256,
    updated_at: Number(r.updated_at),
  });
});
workspaceRoutes.put("/v1/projects/:projectId/files", authMiddleware, async (c) => {
  const projectId = c.req.param("projectId") ?? "";
  const body = await c.req.json<{ path?: string; content_base64?: string }>();
  const path = normalizePath(body.path ?? "");
  if (!path || typeof body.content_base64 !== "string") {
    return c.json({ detail: "path and content_base64 required" }, 400);
  }
  const bytes = decodeBase64(body.content_base64);
  if (!bytes) return c.json({ detail: "invalid base64 content" }, 400);
  if (bytes.length > MAX_FILE_BYTES) return c.json({ detail: "file too large" }, 413);

  await ensureWorkspaceSchema(c.env);
  if (!(await projectExists(c.env, projectId))) {
    return c.json({ detail: "project not found" }, 404);
  }
  const db = sql(c.env);
  const totals = await db`SELECT COALESCE(SUM(size_bytes), 0)::bigint AS total
    FROM workspace_files WHERE project_id = ${projectId} AND path <> ${path}`;
  const total = Number(totals[0]?.total ?? 0) + bytes.length;
  if (total > MAX_WORKSPACE_BYTES) return c.json({ detail: "workspace too large" }, 413);
  const digest = await sha256(bytes);
  const ts = nowMs();
  await db`INSERT INTO workspace_files (project_id, path, content_base64, size_bytes, sha256, updated_at)
    VALUES (${projectId}, ${path}, ${body.content_base64}, ${bytes.length}, ${digest}, ${ts})
    ON CONFLICT (project_id, path) DO UPDATE SET
      content_base64 = EXCLUDED.content_base64,
      size_bytes = EXCLUDED.size_bytes,
      sha256 = EXCLUDED.sha256,
      updated_at = EXCLUDED.updated_at`;
  await db`UPDATE projects SET updated_at = ${ts} WHERE id = ${projectId}`;
  return c.json({ path, size_bytes: bytes.length, sha256: digest, updated_at: ts });
});

workspaceRoutes.delete("/v1/projects/:projectId/file", authMiddleware, async (c) => {
  const projectId = c.req.param("projectId") ?? "";
  const path = normalizePath(c.req.query("path") ?? "");
  if (!path) return c.json({ detail: "invalid path" }, 400);
  await ensureWorkspaceSchema(c.env);
  const db = sql(c.env);
  const rows = await db`DELETE FROM workspace_files
    WHERE project_id = ${projectId} AND path = ${path} RETURNING path`;
  if (rows.length === 0) return c.json({ detail: "file not found" }, 404);
  await db`UPDATE projects SET updated_at = ${nowMs()} WHERE id = ${projectId}`;
  return c.json({ status: "deleted", path });
});
workspaceRoutes.get("/v1/internal/projects/:projectId/workspace", callbackAuth, async (c) => {
  const projectId = c.req.param("projectId") ?? "";
  await ensureWorkspaceSchema(c.env);
  if (!(await projectExists(c.env, projectId))) {
    return c.json({ detail: "project not found" }, 404);
  }
  const db = sql(c.env);
  const rows = await db`SELECT path, content_base64, size_bytes, sha256, updated_at
    FROM workspace_files WHERE project_id = ${projectId} ORDER BY path`;
  const total = rows.reduce((sum, row) => sum + Number(row.size_bytes ?? 0), 0);
  if (rows.length > MAX_FILES || total > MAX_WORKSPACE_BYTES) {
    return c.json({ detail: "workspace exceeds runner limits" }, 413);
  }
  return c.json({ project_id: projectId, files: rows, total_bytes: total });
});

workspaceRoutes.post("/v1/internal/projects/:projectId/workspace/sync", callbackAuth, async (c) => {
  const projectId = c.req.param("projectId") ?? "";
  const body = await c.req.json<{
    files?: Array<{ path: string; content_base64: string }>;
    deleted_paths?: string[];
  }>();
  const files = body.files ?? [];
  if (files.length > MAX_FILES) return c.json({ detail: "too many files" }, 413);
  const prepared: Array<{ path: string; content_base64: string; size: number; sha: string }> = [];
  let total = 0;
  for (const item of files) {
    const path = normalizePath(item.path ?? "");
    const bytes = decodeBase64(item.content_base64 ?? "");
    if (!path || !bytes) return c.json({ detail: "invalid workspace file" }, 400);
    if (bytes.length > MAX_FILE_BYTES) return c.json({ detail: `file too large: ${path}` }, 413);
    total += bytes.length;
    if (total > MAX_WORKSPACE_BYTES) return c.json({ detail: "workspace too large" }, 413);
    prepared.push({
      path,
      content_base64: item.content_base64,
      size: bytes.length,
      sha: await sha256(bytes),
    });
  }

  await ensureWorkspaceSchema(c.env);
  if (!(await projectExists(c.env, projectId))) {
    return c.json({ detail: "project not found" }, 404);
  }
  const db = sql(c.env);
  const ts = nowMs();
  for (const item of prepared) {
    await db`INSERT INTO workspace_files (project_id, path, content_base64, size_bytes, sha256, updated_at)
      VALUES (${projectId}, ${item.path}, ${item.content_base64}, ${item.size}, ${item.sha}, ${ts})
      ON CONFLICT (project_id, path) DO UPDATE SET
        content_base64 = EXCLUDED.content_base64,
        size_bytes = EXCLUDED.size_bytes,
        sha256 = EXCLUDED.sha256,
        updated_at = EXCLUDED.updated_at`;
  }

  for (const raw of body.deleted_paths ?? []) {
    const path = normalizePath(raw);
    if (!path) continue;
    await db`DELETE FROM workspace_files WHERE project_id = ${projectId} AND path = ${path}`;
  }
  await db`UPDATE projects SET updated_at = ${ts} WHERE id = ${projectId}`;
  return c.json({ status: "synced", files: prepared.length, total_bytes: total, updated_at: ts });
});
workspaceRoutes.get("/v1/internal/jobs/:jobId/context", callbackAuth, async (c) => {
  const jobId = c.req.param("jobId") ?? "";
  const db = sql(c.env);
  const rows = await db`SELECT id, project_id, goal, job_type, state, checkpoint_stage, cancel_requested
    FROM jobs WHERE id = ${jobId}`;
  if (rows.length === 0) return c.json({ detail: "job not found" }, 404);
  return c.json(rows[0]);
});
