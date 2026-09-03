import type { Context, Next } from "hono";
import type { Env } from "./types";

export const PHONE_AGENT_OIDC_AUDIENCE = "hassan-phone-agent";
export const PHONE_AGENT_CONTROL_REPO = "Hassankakaee333/FMK-AI-BRIDGE";
export const PHONE_AGENT_CONTROL_ACTOR = "Hassankakaee333";
const GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com";
const CLOCK_SKEW_SECONDS = 60;

type GitHubOidcClaims = {
  iss?: string;
  aud?: string | string[];
  exp?: number;
  nbf?: number;
  iat?: number;
  repository?: string;
  repository_owner?: string;
  repository_visibility?: string;
  actor?: string;
  event_name?: string;
  [key: string]: unknown;
};

type GitHubJwk = JsonWebKey & { kid?: string };
type JwksResponse = { keys?: GitHubJwk[] };

let cachedJwks: { at: number; keys: GitHubJwk[] } | null = null;

function decodeBase64UrlBytes(value: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const binary = atob(padded);
  return Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
}

function decodeBase64UrlJson<T>(value: string): T {
  const bytes = decodeBase64UrlBytes(value);
  return JSON.parse(new TextDecoder().decode(bytes)) as T;
}

export function validateGitHubOidcClaims(
  claims: GitHubOidcClaims,
  nowSeconds = Math.floor(Date.now() / 1000),
): boolean {
  const audience = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (claims.iss !== GITHUB_OIDC_ISSUER) return false;
  if (!audience.includes(PHONE_AGENT_OIDC_AUDIENCE)) return false;
  if (claims.repository !== PHONE_AGENT_CONTROL_REPO) return false;
  if (claims.repository_owner !== PHONE_AGENT_CONTROL_ACTOR) return false;
  if (claims.repository_visibility !== "private") return false;
  if (claims.actor !== PHONE_AGENT_CONTROL_ACTOR) return false;
  if (claims.event_name !== "issues") return false;
  if (typeof claims.exp !== "number" || claims.exp < nowSeconds - CLOCK_SKEW_SECONDS) return false;
  if (typeof claims.nbf === "number" && claims.nbf > nowSeconds + CLOCK_SKEW_SECONDS) return false;
  if (typeof claims.iat === "number" && claims.iat > nowSeconds + CLOCK_SKEW_SECONDS) return false;
  return true;
}

async function loadJwks(): Promise<GitHubJwk[]> {
  const now = Date.now();
  if (cachedJwks && now - cachedJwks.at < 10 * 60_000) return cachedJwks.keys;
  const response = await fetch(`${GITHUB_OIDC_ISSUER}/.well-known/jwks`);
  if (!response.ok) throw new Error(`GitHub OIDC JWKS HTTP ${response.status}`);
  const body = (await response.json()) as JwksResponse;
  const keys = body.keys ?? [];
  if (keys.length === 0) throw new Error("GitHub OIDC JWKS is empty");
  cachedJwks = { at: now, keys };
  return keys;
}

export async function verifyGitHubActionsOidc(token: string): Promise<GitHubOidcClaims | null> {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const header = decodeBase64UrlJson<{ alg?: string; kid?: string }>(parts[0]);
    const claims = decodeBase64UrlJson<GitHubOidcClaims>(parts[1]);
    if (header.alg !== "RS256" || !header.kid || !validateGitHubOidcClaims(claims)) return null;

    const jwk = (await loadJwks()).find((candidate) => candidate.kid === header.kid);
    if (!jwk) return null;
    const key = await crypto.subtle.importKey(
      "jwk",
      jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    const signed = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
    const signature = decodeBase64UrlBytes(parts[2]);
    const valid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, signature, signed);
    return valid ? claims : null;
  } catch {
    return null;
  }
}

export async function phoneAgentOidcAuth(c: Context<{ Bindings: Env }>, next: Next) {
  const header = c.req.header("Authorization");
  if (!header?.startsWith("Bearer ")) return c.json({ detail: "Missing GitHub OIDC token" }, 401);
  const token = header.slice("Bearer ".length).trim();
  const claims = await verifyGitHubActionsOidc(token);
  if (!claims) return c.json({ detail: "Invalid GitHub OIDC identity" }, 403);
  await next();
}
