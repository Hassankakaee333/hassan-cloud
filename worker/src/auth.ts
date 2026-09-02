import type { Context, Next } from "hono";
import { hashToken } from "./db";
import type { Env } from "./types";

export async function authMiddleware(c: Context<{ Bindings: Env }>, next: Next) {
  const header = c.req.header("Authorization");
  if (!header?.startsWith("Bearer ")) {
    return c.json({ detail: "Missing Bearer token" }, 401);
  }
  const raw = header.slice("Bearer ".length).trim();
  const tokenHash = await hashToken(raw);
  const { neon } = await import("@neondatabase/serverless");
  const db = neon(c.env.DATABASE_URL);
  const rows = await db`SELECT id FROM api_tokens WHERE token_hash = ${tokenHash} AND revoked_at IS NULL`;
  if (rows.length === 0) {
    return c.json({ detail: "Invalid or revoked token" }, 403);
  }
  await next();
}

export async function callbackAuth(c: Context<{ Bindings: Env }>, next: Next) {
  const secret = c.req.header("X-Hassan-Callback-Secret");
  if (!secret || secret !== c.env.GITHUB_CALLBACK_SECRET) {
    return c.json({ detail: "Forbidden" }, 403);
  }
  await next();
}
