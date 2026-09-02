import { neon } from "@neondatabase/serverless";
import type { Env } from "./types";

/** Neon serverless HTTP driver — official, works in Cloudflare Workers (no TCP psycopg). */
export function sql(env: Env) {
  return neon(env.DATABASE_URL);
}

export async function healthCheck(env: Env): Promise<boolean> {
  try {
    const db = sql(env);
    const rows = await db`SELECT 1 AS ok`;
    return rows.length > 0;
  } catch {
    return false;
  }
}

export async function countActiveTokens(env: Env): Promise<number> {
  const db = sql(env);
  const rows = await db`SELECT COUNT(*)::int AS c FROM api_tokens WHERE revoked_at IS NULL`;
  return Number(rows[0]?.c ?? 0);
}

export async function ensureBootstrapToken(env: Env): Promise<void> {
  const raw = env.HASSAN_BOOTSTRAP_TOKEN?.trim();
  if (!raw) return;
  const db = sql(env);
  const existing = await db`SELECT id FROM api_tokens LIMIT 1`;
  if (existing.length > 0) return;
  const hash = await hashToken(raw);
  const id = crypto.randomUUID();
  const now = Date.now();
  await db`INSERT INTO api_tokens (id, token_hash, label, device_id, created_at, revoked_at)
    VALUES (${id}, ${hash}, ${"bootstrap"}, ${null}, ${now}, ${null})`;
}

export async function hashToken(raw: string): Promise<string> {
  const data = new TextEncoder().encode(raw);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function newId(): string {
  return crypto.randomUUID();
}

export function nowMs(): number {
  return Date.now();
}
