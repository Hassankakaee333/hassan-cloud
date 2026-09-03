import { describe, expect, it } from "vitest";
import { PHONE_AGENT_OIDC_AUDIENCE, PHONE_AGENT_CONTROL_ACTOR, PHONE_AGENT_CONTROL_REPO, validateGitHubOidcClaims } from "./github-oidc";
import { normalizePhoneAgentCommand } from "./phone-agent";

const NOW_MS = 1_800_000_000_000;
const NOW_SECONDS = Math.floor(NOW_MS / 1000);

function validClaims(overrides: Record<string, unknown> = {}) {
  return {
    iss: "https://token.actions.githubusercontent.com",
    aud: PHONE_AGENT_OIDC_AUDIENCE,
    exp: NOW_SECONDS + 300,
    iat: NOW_SECONDS - 5,
    repository: PHONE_AGENT_CONTROL_REPO,
    repository_owner: PHONE_AGENT_CONTROL_ACTOR,
    repository_visibility: "private",
    actor: PHONE_AGENT_CONTROL_ACTOR,
    event_name: "issues",
    ...overrides,
  };
}

describe("phone-agent bridge validation", () => {
  it("accepts only the private owner control repository", () => {
    expect(validateGitHubOidcClaims(validClaims(), NOW_SECONDS)).toBe(true);
    expect(validateGitHubOidcClaims(validClaims({ repository: "someone/public" }), NOW_SECONDS)).toBe(false);
    expect(validateGitHubOidcClaims(validClaims({ actor: "someone" }), NOW_SECONDS)).toBe(false);
    expect(validateGitHubOidcClaims(validClaims({ repository_visibility: "public" }), NOW_SECONDS)).toBe(false);
  });

  it("rejects wrong audience, wrong event, and expired identities", () => {
    expect(validateGitHubOidcClaims(validClaims({ aud: "other" }), NOW_SECONDS)).toBe(false);
    expect(validateGitHubOidcClaims(validClaims({ event_name: "push" }), NOW_SECONDS)).toBe(false);
    expect(validateGitHubOidcClaims(validClaims({ exp: NOW_SECONDS - 120 }), NOW_SECONDS)).toBe(false);
  });

  it("normalizes a safe command and supplies a short expiry", () => {
    const command = normalizePhoneAgentCommand({ action: "open_app", packageName: "com.example.app" }, NOW_MS);
    expect(command.action).toBe("OPEN_APP");
    expect(command.packageName).toBe("com.example.app");
    expect(command.expiresAtEpochMs).toBeGreaterThan(NOW_MS);
    expect(command.expiresAtEpochMs).toBeLessThanOrEqual(NOW_MS + 15 * 60_000);
    expect(command.id).toMatch(/^gh-/);
  });

  it("rejects unsupported and malformed state-changing commands", () => {
    expect(() => normalizePhoneAgentCommand({ action: "SHELL", text: "rm" }, NOW_MS)).toThrow(/unsupported action/);
    expect(() => normalizePhoneAgentCommand({ action: "OPEN_APP", packageName: "bad package" }, NOW_MS)).toThrow(/packageName/);
    expect(() => normalizePhoneAgentCommand({ action: "TAP", x: 20 }, NOW_MS)).toThrow(/x and y/);
    expect(() => normalizePhoneAgentCommand({ action: "SWIPE", x: 1, y: 2, endX: 3 }, NOW_MS)).toThrow(/endY/);
  });

  it("preserves explicit confirmation requirements", () => {
    const command = normalizePhoneAgentCommand({ action: "SET_TEXT", text: "hello", requiresConfirmation: true }, NOW_MS);
    expect(command.requiresConfirmation).toBe(true);
  });
});
