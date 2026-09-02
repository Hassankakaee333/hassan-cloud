import { describe, expect, it } from "vitest";
import { hashToken, newId, nowMs } from "./db";

describe("database helpers", () => {
  it("hashes bearer tokens with SHA-256", async () => {
    expect(await hashToken("abc")).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });

  it("generates UUID identifiers", () => {
    expect(newId()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  });

  it("returns a current millisecond timestamp", () => {
    const before = Date.now();
    const value = nowMs();
    expect(value).toBeGreaterThanOrEqual(before);
    expect(value).toBeLessThanOrEqual(Date.now());
  });
});
