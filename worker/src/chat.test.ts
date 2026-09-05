import { afterEach, describe, expect, it, vi } from "vitest";
import { chatConfigFlags, generateCoderText, resolveChat } from "./chat";
import type { Env } from "./types";

const envWithLegacyPaidKeys = {
  OPENAI_API_KEY: "must-not-be-used",
  GEMINI_API_KEY: "must-not-be-used",
  DEEPSEEK_API_KEY: "must-not-be-used",
} as unknown as Env;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("truth-based chat provider gate", () => {
  it("never impersonates ChatGPT and never calls a paid API even if a legacy key exists", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await resolveChat(envWithLegacyPaidKeys, "chatgpt", [
      { role: "user", content: "مرحبا" },
    ]);

    expect(result.status).toBe("NOT_CONFIGURED");
    expect(result.provider).toBe("chatgpt");
    expect(result.model).toBe("unverified-account-runtime");
    expect(result.answer).toContain("غير متصل");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("never impersonates Gemini, DeepSeek or Claude", async () => {
    for (const provider of ["gemini", "deepseek", "claude"]) {
      const result = await resolveChat(envWithLegacyPaidKeys, provider, [
        { role: "user", content: "من أنت؟" },
      ]);
      expect(result.status).toBe("NOT_CONFIGURED");
      expect(result.provider).toBe(provider);
      expect(result.model).toBe("unverified-account-runtime");
      expect(result.answer).not.toContain(`أنا ${provider}`);
    }
  });

  it("keeps local fallback labeled only as Frishta", async () => {
    const result = await resolveChat({} as Env, "auto", [
      { role: "user", content: "من أنت؟" },
    ]);

    expect(result.status).toBe("LOCAL_FALLBACK");
    expect(result.provider).toBe("frishta");
    expect(result.model).toBe("local-frishta");
    expect(result.answer).toContain("Frishta AI");
    expect(result.answer).toContain("ليس ردًا من مزود خارجي");
  });

  it("disables provider-backed coder API regardless of legacy Gemini key", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await generateCoderText(envWithLegacyPaidKeys, "write code");

    expect(result.status).toBe("NOT_CONFIGURED");
    expect(result.model).toBe("disabled-paid-api");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("reports policy state rather than secret presence", () => {
    const flags = chatConfigFlags(envWithLegacyPaidKeys);
    expect(flags.openai_configured).toBe(false);
    expect(flags.gemini_configured).toBe(false);
    expect(flags.deepseek_configured).toBe(false);
    expect(flags.paid_api_execution_allowed).toBe(false);
    expect(flags.account_runtime_verified).toBe(false);
  });
});
