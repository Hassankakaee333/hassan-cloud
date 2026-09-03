import { Hono } from "hono";
import { newId, nowMs, sql } from "./db";
import { phoneAgentOidcAuth } from "./github-oidc";
import type { Env } from "./types";

export const phoneAgentRoutes = new Hono<{ Bindings: Env }>();

export const PHONE_AGENT_PROJECT_NAME = "Hassan Phone Agent";
const INBOX_PREFIX = "phone-agent/inbox/";
const OUTBOX_PREFIX = "phone-agent/outbox/";
const HEARTBEAT_PATH = "phone-agent/heartbeat.json";
const MAX_EXPIRY_MS = 15 * 60_000;
const DEFAULT_EXPIRY_MS = 10 * 60_000;

export const PHONE_AGENT_ACTIONS = new Set([
  "PING",
  "UI_TREE",
  "OPEN_APP",
  "HOME",
  "BACK",
  "RECENTS",
  "NOTIFICATIONS",
  "QUICK_SETTINGS",
  "CLICK_TEXT",
  "SET_TEXT",
  "TAP",
  "SWIPE",
  "SCROLL_FORWARD",
  "SCROLL_BACKWARD",
  "SCREENSHOT",
]);

type RawPhoneCommand = {
  id?: unknown;
  action?: unknown;
  packageName?: unknown;
  targetText?: unknown;
  text?: unknown;
  x?: unknown;
  y?: unknown;
  endX?: unknown;
  endY?: unknown;
  durationMs?: unknown;
  requiresConfirmation?: unknown;
  expiresAtEpochMs?: unknown;
};

export type PhoneAgentCommand = {
  id: string;
  action: string;
  packageName?: string;
  targetText?: string;
  text?: string;
  x?: number;
  y?: number;
  endX?: number;
  endY?: number;
  durationMs: number;
  requiresConfirmation: boolean;
  expiresAtEpochMs: number;
};

function optionalString(value: unknown, max: number, label: string): string | undefined {
  if (value == null) return undefined;
  if (typeof value !== "string") throw new Error(`${label} must be a string`);
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  if (trimmed.length > max) throw new Error(`${label} is too long`);
  return trimmed;
}

function optionalCoordinate(value: unknown, label: string): number | undefined {
  if (value == null) return undefined;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 20_000) {
    throw new Error(`${label} is invalid`);
  }
  return value;
}

function safeCommandId(value: unknown): string {
  if (value == null || value === "") return `gh-${newId()}`;
  if (typeof value !== "string" || !/^[A-Za-z0-9._-]{1,96}$/.test(value)) {
    throw new Error("id is invalid");
  }
  return value;
}

export function normalizePhoneAgentCommand(
  raw: RawPhoneCommand,
  now = Date.now(),
): PhoneAgentCommand {
  if (!raw || typeof raw !== "object") throw new Error("command must be an object");
  if (typeof raw.action !== "string") throw new Error("action is required");
  const action = raw.action.trim().toUpperCase();
  if (!PHONE_AGENT_ACTIONS.has(action)) throw new Error(`unsupported action: ${action}`);

  const packageName = optionalString(raw.packageName, 220, "packageName");
  if (packageName && !/^[A-Za-z0-9._]+$/.test(packageName)) throw new Error("packageName is invalid");
  if (action === "OPEN_APP" && !packageName) throw new Error("packageName is required for OPEN_APP");

  const targetText = optionalString(raw.targetText, 500, "targetText");
  const text = optionalString(raw.text, 5_000, "text");
  const x = optionalCoordinate(raw.x, "x");
  const y = optionalCoordinate(raw.y, "y");
  const endX = optionalCoordinate(raw.endX, "endX");
  const endY = optionalCoordinate(raw.endY, "endY");

  if (action === "CLICK_TEXT" && !targetText) throw new Error("targetText is required for CLICK_TEXT");
  if (action === "SET_TEXT" && text == null) throw new Error("text is required for SET_TEXT");
  if (action === "TAP" && (x == null || y == null)) throw new Error("x and y are required for TAP");
  if (action === "SWIPE" && (x == null || y == null || endX == null || endY == null)) {
    throw new Error("x, y, endX and endY are required for SWIPE");
  }

  const durationMs = raw.durationMs == null ? 350 : Number(raw.durationMs);
  if (!Number.isFinite(durationMs) || durationMs < 80 || durationMs > 5_000) {
    throw new Error("durationMs is invalid");
  }

  const requestedExpiry = raw.expiresAtEpochMs == null ? 0 : Number(raw.expiresAtEpochMs);
  const expiresAtEpochMs = requestedExpiry > 0 ? requestedExpiry : now + DEFAULT_EXPIRY_MS;
  if (!Number.isFinite(expiresAtEpochMs) || expiresAtEpochMs <= now || expiresAtEpochMs > now + MAX_EXPIRY_MS) {
    throw new Error("expiresAtEpochMs must be within the next 15 minutes");
  }

  return {
    id: safeCommandId(raw.id),
    action,
    packageName,
    targetText,
    text,
    x,
    y,
    endX,
    endY,
    durationMs: Math.round(durationMs),
    requiresConfirmation: raw.requiresConfirmation === true,
    expiresAtEpochMs: Math.round(expiresAtEpochMs),
  };
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
  await db`CREATE INDEX IF NOT EXISTS idx_workspace_files_project ON workspace_files(project_id)`;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, Math.min(i + 0x8000, bytes.length)));
  }
  return btoa(binary);
}

function jsonToBase64(value: unknown): { encoded: string; bytes: Uint8Array } {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  return { encoded: bytesToBase64(bytes), bytes };
}

function base64ToJson(value: string): unknown {
  const binary = atob(value);
  const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function findPhoneProjectId(env: Env): Promise<string | null> {
  const db = sql(env);
  const rows = await db`SELECT id FROM projects WHERE name = ${PHONE_AGENT_PROJECT_NAME}
    ORDER BY updated_at DESC LIMIT 1`;
  return rows.length > 0 ? String(rows[0].id) : null;
}

async function ensurePhoneProjectId(env: Env): Promise<string> {
  const existing = await findPhoneProjectId(env);
  if (existing) return existing;
  const id = newId();
  const ts = nowMs();
  const db = sql(env);
  await db`INSERT INTO projects (id, name, description, workspace_path, created_at, updated_at)
    VALUES (${id}, ${PHONE_AGENT_PROJECT_NAME}, ${"Private cloud command/results workspace for Hassan Phone Agent"}, ${`workspace/${id}`}, ${ts}, ${ts})`;
  return id;
}

async function putWorkspaceJson(env: Env, projectId: string, path: string, value: unknown): Promise<void> {
  await ensureWorkspaceSchema(env);
  const { encoded, bytes } = jsonToBase64(value);
  if (bytes.length > 256 * 1024) throw new Error("phone-agent payload too large");
  const digest = await sha256(bytes);
  const ts = nowMs();
  const db = sql(env);
  await db`INSERT INTO workspace_files (project_id, path, content_base64, size_bytes, sha256, updated_at)
    VALUES (${projectId}, ${path}, ${encoded}, ${bytes.length}, ${digest}, ${ts})
    ON CONFLICT (project_id, path) DO UPDATE SET
      content_base64 = EXCLUDED.content_base64,
      size_bytes = EXCLUDED.size_bytes,
      sha256 = EXCLUDED.sha256,
      updated_at = EXCLUDED.updated_at`;
  await db`UPDATE projects SET updated_at = ${ts} WHERE id = ${projectId}`;
}

async function readWorkspaceJson(env: Env, projectId: string, path: string): Promise<unknown | null> {
  await ensureWorkspaceSchema(env);
  const db = sql(env);
  const rows = await db`SELECT content_base64 FROM workspace_files
    WHERE project_id = ${projectId} AND path = ${path}`;
  if (rows.length === 0) return null;
  return base64ToJson(String(rows[0].content_base64));
}

phoneAgentRoutes.post("/v1/phone-agent/bridge/commands", phoneAgentOidcAuth, async (c) => {
  let command: PhoneAgentCommand;
  try {
    command = normalizePhoneAgentCommand(await c.req.json<RawPhoneCommand>());
  } catch (error) {
    return c.json({ detail: error instanceof Error ? error.message : "invalid command" }, 400);
  }
  const projectId = await ensurePhoneProjectId(c.env);
  await putWorkspaceJson(c.env, projectId, `${INBOX_PREFIX}${command.id}.json`, command);
  return c.json({ id: command.id, status: "QUEUED", project_id: projectId, expires_at: command.expiresAtEpochMs }, 202);
});

phoneAgentRoutes.get("/v1/phone-agent/bridge/results/:commandId", phoneAgentOidcAuth, async (c) => {
  const commandId = c.req.param("commandId") ?? "";
  if (!/^[A-Za-z0-9._-]{1,96}$/.test(commandId)) return c.json({ detail: "invalid command id" }, 400);
  const projectId = await findPhoneProjectId(c.env);
  if (!projectId) return c.json({ id: commandId, status: "PENDING" }, 202);
  const result = await readWorkspaceJson(c.env, projectId, `${OUTBOX_PREFIX}${commandId}.json`);
  if (!result) return c.json({ id: commandId, status: "PENDING" }, 202);
  return c.json(result);
});

phoneAgentRoutes.get("/v1/phone-agent/bridge/heartbeat", phoneAgentOidcAuth, async (c) => {
  const projectId = await findPhoneProjectId(c.env);
  if (!projectId) return c.json({ online: false, status: "WAITING_FOR_PHONE" }, 202);
  const heartbeat = await readWorkspaceJson(c.env, projectId, HEARTBEAT_PATH);
  if (!heartbeat) return c.json({ online: false, status: "WAITING_FOR_PHONE" }, 202);
  return c.json(heartbeat);
});
