import { describe, expect, it } from "vitest";
import { PROVIDERS, providersForCapability } from "./providers";

describe("free-first provider registry", () => {
  it("contains only free providers", () => {
    expect(PROVIDERS.length).toBeGreaterThan(0);
    expect(PROVIDERS.every((provider) => provider.cost_type === "FREE")).toBe(true);
  });

  it("keeps paid chat honestly unconfigured", () => {
    expect(providersForCapability("chat")).toEqual([
      expect.objectContaining({ id: "hassan-honest-chat", status: "NOT_CONFIGURED" }),
    ]);
  });

  it("routes Android builds to the verified GitHub Actions worker", () => {
    expect(providersForCapability("android_build")).toEqual([
      expect.objectContaining({ id: "github-actions-coding-worker", status: "WORKING" }),
    ]);
  });

  it("routes candidate self-improve to GitHub Actions worker", () => {
    expect(providersForCapability("candidate_self_improve")).toEqual([
      expect.objectContaining({ id: "github-actions-coding-worker", status: "WORKING" }),
    ]);
  });
});