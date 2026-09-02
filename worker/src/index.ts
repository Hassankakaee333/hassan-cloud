import { Hono } from "hono";
import { cors } from "hono/cors";
import { authMiddleware, callbackAuth } from "./auth";
import { cancelGitHubRun, dispatchGitHubWorkflow, downloadGitHubArtifactFile } from "./github";
import { countActiveTokens, ensureBootstrapToken, healthCheck, hashToken, newId, nowMs, sql } from "./db";
import type { Env } from "./types";

const app = new Hono<{ Bindings: Env }>();

app.use(
  "*",
  cors({
    origin: (origin) => origin ?? "*",
    allowMethods: ["GET", "POST", "DELETE", "OPTIONS"],
    allowHeaders: ["Authorization", "Content-Type", "X-Hassan-Callback-Secret"],
    maxAge: 86400,
  }),
);

app.use("*", async (c, next) => {
  await ensureBootstrapToken(c.env);
  await next();
});

// --- Health ---

app.get("/v1/health", async (c) => {
  const dbOk = await healthCheck(c.env);
  const tokens = await countActiveTokens(c.env);
  let githubOk = false;
  try {
    const [owner, repo] = c.env.GITHUB_REPO.split("/");
    const r = await fetch(`https://api.github.com/repos/${owner}/${repo}`, {
      headers: { Authorization: `Bearer ${c.env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json" },
    });
    githubOk = r.ok;
  } catch {
    githubOk = false;
  }
  return c.json({
    status: dbOk && githubOk ? "ok" : "degraded",
    service: "hassan-cloud",
    version: c.env.HASSAN_VERSION,
    env: c.env.HASSAN_ENV,
    database: { backend: "neon", status: dbOk ? "WORKING" : "FAILED" },
    job_runtime: { mode: "github_actions", status: githubOk ? "WORKING" : "FAILED" },
    artifact_store: c.env.ARTIFACT_BACKEND,
    auth: { active_tokens: tokens, configured: tokens > 0 },
    openai_configured: false,
    chat_status: "NOT_CONFIGURED",
  });
});

// --- Auth ---

app.post("/v1/auth/verify", authMiddleware, (c) => c.json({ status: "valid" }));

app.post("/v1/auth/tokens", authMiddleware, async (c) => {
  const body = await c.req.json<{ label?: string; device_id?: string }>();
  const raw = crypto.randomUUID().replace(/-/g, "") + crypto.randomUUID().replace(/-/g, "").slice(0, 8);
  const id = newId();
  const tokenHash = await hashToken(raw);
  const ts = nowMs();
  const db = sql(c.env);
  await db`INSERT INTO api_tokens (id, token_hash, label, device_id, created_at, revoked_at)
    VALUES (${id}, ${tokenHash}, ${body.label ?? "device"}, ${body.device_id ?? null}, ${ts}, ${null})`;
  return c.json({ id, token: raw, label: body.label ?? "device" });
});

app.delete("/v1/auth/tokens/:tokenId", authMiddleware, async (c) => {
  const tokenId = c.req.param("tokenId");
  const db = sql(c.env);
  const rows = await db`UPDATE api_tokens SET revoked_at = ${nowMs()} WHERE id = ${tokenId} AND revoked_at IS NULL RETURNING id`;
  if (rows.length === 0) return c.json({ detail: "token not found or already revoked" }, 404);
  return c.json({ status: "revoked", id: tokenId });
});

// --- Projects ---

app.get("/v1/projects", authMiddleware, async (c) => {
  const db = sql(c.env);
  const rows = await db`SELECT * FROM projects ORDER BY created_at DESC`;
  return c.json(rows);
});

app.post("/v1/projects", authMiddleware, async (c) => {
  const body = await c.req.json<{ name: string; description?: string }>();
  const id = newId();
  const ts = nowMs();
  const db = sql(c.env);
  await db`INSERT INTO projects (id, name, description, workspace_path, created_at, updated_at)
    VALUES (${id}, ${body.name}, ${body.description ?? ""}, ${`workspace/${id}`}, ${ts}, ${ts})`;
  const rows = await db`SELECT * FROM projects WHERE id = ${id}`;
  return c.json(rows[0]);
});

app.get("/v1/projects/:projectId", authMiddleware, async (c) => {
  const db = sql(c.env);
  const rows = await db`SELECT * FROM projects WHERE id = ${c.req.param("projectId")}`;
  if (rows.length === 0) return c.json({ detail: "project not found" }, 404);
  return c.json(rows[0]);
});

app.get("/v1/projects/:projectId/workspace", authMiddleware, async (c) => {
  const pid = c.req.param("projectId");
  const db = sql(c.env);
  const project = await db`SELECT * FROM projects WHERE id = ${pid}`;
  if (project.length === 0) return c.json({ detail: "project not found" }, 404);
  const conversations = await db`SELECT * FROM conversations WHERE project_id = ${pid}`;
  const jobs = await db`SELECT * FROM jobs WHERE project_id = ${pid} ORDER BY created_at DESC`;
  const artifacts = await db`SELECT id, project_id, job_id, conversation_id, name, mime_type, size_bytes, created_at FROM artifacts WHERE project_id = ${pid}`;
  return c.json({ project: project[0], conversations, jobs, artifacts });
});

// --- Jobs ---

app.post("/v1/jobs", authMiddleware, async (c) => {
  const body = await c.req.json<{
    project_id: string;
    conversation_id?: string;
    goal: string;
    job_type?: string;
    idempotency_key?: string;
  }>();
  const db = sql(c.env);
  const project = await db`SELECT id FROM projects WHERE id = ${body.project_id}`;
  if (project.length === 0) return c.json({ detail: "project not found" }, 404);

  if (body.idempotency_key) {
    const existing = await db`SELECT * FROM jobs WHERE idempotency_key = ${body.idempotency_key}`;
    if (existing.length > 0) return c.json(existing[0]);
  }

  const id = newId();
  const ts = nowMs();
  await db`INSERT INTO jobs (
    id, project_id, conversation_id, goal, job_type, state, result_summary, log,
    created_at, updated_at, idempotency_key, checkpoint_stage, cancel_requested,
    dispatch_attempt, github_workflow
  ) VALUES (
    ${id}, ${body.project_id}, ${body.conversation_id ?? null}, ${body.goal},
    ${body.job_type ?? "general"}, ${"QUEUED"}, ${null}, ${""},
    ${ts}, ${ts}, ${body.idempotency_key ?? null}, ${""}, ${0},
    ${0}, ${c.env.GITHUB_WORKFLOW_FILE}
  )`;
  let rows = await db`SELECT * FROM jobs WHERE id = ${id}`;

  // Dispatch GitHub Actions
  await db`UPDATE jobs SET state = ${"DISPATCHING"}, updated_at = ${nowMs()} WHERE id = ${id}`;
  const dispatch = await dispatchGitHubWorkflow(c.env, id, body.project_id, body.job_type ?? "general");
  const attempt = 1;
  const dispatchTs = nowMs();
  if (dispatch.ok) {
    await db`UPDATE jobs SET state = ${"QUEUED"}, github_run_id = ${dispatch.runId ?? null},
      dispatch_attempt = ${attempt}, last_dispatch_at = ${dispatchTs}, updated_at = ${dispatchTs},
      log = ${"[worker] GitHub Actions dispatched\n"} WHERE id = ${id}`;
  } else {
    await db`UPDATE jobs SET state = ${"FAILED"}, failure_reason = ${dispatch.error ?? "dispatch failed"},
      log = ${`[worker] dispatch failed: ${dispatch.error}\n`}, updated_at = ${dispatchTs} WHERE id = ${id}`;
  }
  rows = await db`SELECT * FROM jobs WHERE id = ${id}`;
  return c.json(rows[0]);
});

app.get("/v1/jobs", authMiddleware, async (c) => {
  const db = sql(c.env);
  const rows = await db`SELECT * FROM jobs ORDER BY created_at DESC`;
  return c.json(rows);
});

app.get("/v1/jobs/:jobId", authMiddleware, async (c) => {
  const jobId = c.req.param("jobId");
  const db = sql(c.env);
  const jobs = await db`SELECT * FROM jobs WHERE id = ${jobId}`;
  if (jobs.length === 0) return c.json({ detail: "job not found" }, 404);
  const runs = await db`SELECT * FROM agent_runs WHERE job_id = ${jobId} ORDER BY created_at`;
  return c.json({ ...jobs[0], agent_runs: runs });
});

app.post("/v1/jobs/:jobId/cancel", authMiddleware, async (c) => {
  const jobId = c.req.param("jobId");
  const db = sql(c.env);
  const jobs = await db`SELECT * FROM jobs WHERE id = ${jobId}`;
  if (jobs.length === 0) return c.json({ detail: "job not found" }, 404);
  const job = jobs[0];
  const terminal = ["COMPLETED", "FAILED", "CANCELLED"];
  if (terminal.includes(job.state)) return c.json({ detail: "job cannot be cancelled" }, 409);

  const ts = nowMs();
  if (["QUEUED", "DISPATCHING"].includes(job.state)) {
    await db`UPDATE jobs SET state = ${"CANCELLED"}, cancel_requested = 1, updated_at = ${ts},
      log = ${job.log + "[worker] cancelled while queued\n"} WHERE id = ${jobId}`;
  } else {
    await db`UPDATE jobs SET cancel_requested = 1, updated_at = ${ts} WHERE id = ${jobId}`;
    if (job.github_run_id) await cancelGitHubRun(c.env, job.github_run_id);
  }
  const updated = await db`SELECT * FROM jobs WHERE id = ${jobId}`;
  return c.json(updated[0]);
});

// --- Artifacts / files ---

app.get("/v1/artifacts", authMiddleware, async (c) => {
  const projectId = c.req.query("project_id");
  const db = sql(c.env);
  const rows = projectId
    ? await db`SELECT id, project_id, job_id, conversation_id, name, mime_type, size_bytes, created_at FROM artifacts WHERE project_id = ${projectId}`
    : await db`SELECT id, project_id, job_id, conversation_id, name, mime_type, size_bytes, created_at FROM artifacts ORDER BY created_at DESC`;
  return c.json(rows);
});

app.get("/v1/files/:artifactId", authMiddleware, async (c) => {
  const artifactId = c.req.param("artifactId");
  const db = sql(c.env);
  const rows = await db`SELECT * FROM artifacts WHERE id = ${artifactId}`;
  if (rows.length === 0) return c.json({ detail: "artifact not found" }, 404);
  const artifact = rows[0];
  if (artifact.storage_backend === "INLINE_POC" && artifact.storage_key) {
    const db = sql(c.env);
    const inline = await db`SELECT data FROM artifact_inline WHERE artifact_id = ${artifactId}`;
    if (inline.length === 0) return c.json({ detail: "file missing in storage" }, 404);
    const data = inline[0].data as Uint8Array;
    return new Response(data, {
      headers: {
        "Content-Type": artifact.mime_type,
        "Content-Disposition": `attachment; filename="${artifact.name}"`,
      },
    });
  }
  if (artifact.storage_backend === "GITHUB_ACTIONS" && artifact.storage_key) {
    const [runId, artifactName, fileName] = artifact.storage_key.split("|");
    const data = await downloadGitHubArtifactFile(c.env, runId, artifactName, fileName);
    if (!data) return c.json({ detail: "file missing in storage" }, 404);
    return new Response(data, {
      headers: {
        "Content-Type": artifact.mime_type,
        "Content-Disposition": `attachment; filename="${artifact.name}"`,
      },
    });
  }
  return c.json({ detail: "artifact storage not available" }, 404);
});

app.post("/v1/files/upload", authMiddleware, async (c) => {
  return c.json({ detail: "upload via GitHub Actions for POC; direct upload coming soon" }, 501);
});

// --- Chat (honest fallback) ---

app.post("/v1/chat", authMiddleware, async (c) => {
  const body = await c.req.json<{ provider?: string; messages?: Array<{ role: string; content: string }> }>();
  const last = body.messages?.at(-1)?.content ?? "";
  return c.json({
    answer: `Hassan Cloud يعمل.\n\nرسالتك: ${last.slice(0, 200)}\n\nحالة Chat: NOT_CONFIGURED`,
    provider: body.provider ?? "auto",
    model: "hassan-honest",
    status: "NOT_CONFIGURED",
  });
});

// --- Radar ---

const RADAR_SEED = [
  {
    id: "radar-ollama",
    name: "Ollama",
    candidate_type: "llm_runtime",
    source: "github",
    url: "https://github.com/ollama/ollama",
    license: "MIT",
    cost_type: "FREE",
    capabilities: '["local_llm","inference"]',
  },
  {
    id: "radar-openhands",
    name: "OpenHands",
    candidate_type: "coding_agent",
    source: "github",
    url: "https://github.com/All-Hands-AI/OpenHands",
    license: "MIT",
    cost_type: "FREE",
    capabilities: '["coding","agents"]',
  },
];

app.post("/v1/radar/scan", authMiddleware, async (c) => {
  const db = sql(c.env);
  let seeded = 0;
  const ts = nowMs();
  for (const item of RADAR_SEED) {
    const exists = await db`SELECT id FROM radar_candidates WHERE id = ${item.id}`;
    if (exists.length === 0) {
      await db`INSERT INTO radar_candidates (id, name, candidate_type, source, url, license, cost_type, capabilities, status, discovered_at)
        VALUES (${item.id}, ${item.name}, ${item.candidate_type}, ${item.source}, ${item.url}, ${item.license}, ${item.cost_type}, ${item.capabilities}, ${"NEW"}, ${ts})`;
      seeded++;
    }
  }
  const candidates = await db`SELECT * FROM radar_candidates ORDER BY discovered_at DESC`;
  return c.json({ status: "OK", seeded, candidates });
});

app.get("/v1/radar/candidates", authMiddleware, async (c) => {
  const db = sql(c.env);
  const rows = await db`SELECT * FROM radar_candidates ORDER BY discovered_at DESC`;
  return c.json(rows);
});

app.post("/v1/radar/candidates/:candidateId/evaluate", authMiddleware, async (c) => {
  const allowed = new Set(["EVALUATING", "TESTING", "APPROVED", "REJECTED", "INTEGRATED"]);
  const body = await c.req.json<{ status: string; notes?: string }>();
  if (!allowed.has(body.status)) return c.json({ detail: "invalid status" }, 400);
  const cid = c.req.param("candidateId");
  const db = sql(c.env);
  const rows = await db`SELECT * FROM radar_candidates WHERE id = ${cid}`;
  if (rows.length === 0) return c.json({ detail: "candidate not found" }, 404);
  const ts = nowMs();
  await db`UPDATE radar_candidates SET status = ${body.status}, last_evaluated_at = ${ts}, notes = ${body.notes ?? rows[0].notes} WHERE id = ${cid}`;
  const updated = await db`SELECT * FROM radar_candidates WHERE id = ${cid}`;
  return c.json(updated[0]);
});

// --- Internal callbacks (GitHub Actions) ---

app.post("/v1/internal/jobs/:jobId/update", callbackAuth, async (c) => {
  const jobId = c.req.param("jobId");
  const body = await c.req.json<{
    state?: string;
    log_append?: string;
    result_summary?: string;
    checkpoint_stage?: string;
    github_run_id?: string;
    failure_reason?: string;
  }>();
  const db = sql(c.env);
  const jobs = await db`SELECT * FROM jobs WHERE id = ${jobId}`;
  if (jobs.length === 0) return c.json({ detail: "job not found" }, 404);
  const job = jobs[0];
  if (job.cancel_requested) {
    await db`UPDATE jobs SET state = ${"CANCELLED"}, updated_at = ${nowMs()} WHERE id = ${jobId}`;
    return c.json({ status: "cancelled" });
  }
  const newLog = body.log_append ? job.log + body.log_append : job.log;
  const ts = nowMs();
  await db`UPDATE jobs SET
    state = COALESCE(${body.state ?? null}, state),
    log = ${newLog},
    result_summary = COALESCE(${body.result_summary ?? null}, result_summary),
    checkpoint_stage = COALESCE(${body.checkpoint_stage ?? null}, checkpoint_stage),
    github_run_id = COALESCE(${body.github_run_id ?? null}, github_run_id),
    failure_reason = COALESCE(${body.failure_reason ?? null}, failure_reason),
    updated_at = ${ts}
    WHERE id = ${jobId}`;
  const updated = await db`SELECT * FROM jobs WHERE id = ${jobId}`;
  return c.json(updated[0]);
});

app.post("/v1/internal/jobs/:jobId/artifacts", callbackAuth, async (c) => {
  const jobId = c.req.param("jobId");
  const body = await c.req.json<{
    name: string;
    mime_type: string;
    size_bytes: number;
    sha256?: string;
    storage_key?: string;
    storage_backend?: string;
    content_base64?: string;
    project_id?: string;
  }>();
  const db = sql(c.env);
  const jobs = await db`SELECT project_id FROM jobs WHERE id = ${jobId}`;
  if (jobs.length === 0) return c.json({ detail: "job not found" }, 404);
  const id = newId();
  const ts = nowMs();
  const backend = body.storage_backend ?? (body.content_base64 ? "INLINE_POC" : "GITHUB_ACTIONS");
  const storageKey = body.storage_key ?? (backend === "INLINE_POC" ? "inline" : "");
  await db`INSERT INTO artifacts (id, project_id, job_id, name, mime_type, size_bytes, storage_backend, storage_key, sha256, status, created_at)
    VALUES (${id}, ${body.project_id ?? jobs[0].project_id}, ${jobId}, ${body.name}, ${body.mime_type},
    ${body.size_bytes}, ${backend}, ${storageKey}, ${body.sha256 ?? null}, ${"READY"}, ${ts})`;
  if (backend === "INLINE_POC" && body.content_base64) {
    const bytes = Uint8Array.from(atob(body.content_base64), (ch) => ch.charCodeAt(0));
    await db`INSERT INTO artifact_inline (artifact_id, data) VALUES (${id}, ${bytes})`;
  }
  const rows = await db`SELECT id, project_id, job_id, name, mime_type, size_bytes, created_at FROM artifacts WHERE id = ${id}`;
  return c.json(rows[0]);
});

app.post("/v1/internal/jobs/:jobId/agent-runs", callbackAuth, async (c) => {
  const jobId = c.req.param("jobId");
  const body = await c.req.json<{
    agent_id: string;
    agent_role: string;
    status: string;
    input_text: string;
    output_text: string;
    verification_notes?: string;
  }>();
  const id = newId();
  const ts = nowMs();
  const db = sql(c.env);
  await db`INSERT INTO agent_runs (id, job_id, agent_id, agent_role, status, input_text, output_text, verification_notes, created_at, started_at, finished_at)
    VALUES (${id}, ${jobId}, ${body.agent_id}, ${body.agent_role}, ${body.status}, ${body.input_text}, ${body.output_text},
    ${body.verification_notes ?? null}, ${ts}, ${ts}, ${ts})`;
  return c.json({ id, status: body.status });
});

export default app;
