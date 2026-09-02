import { afterEach, describe, expect, it, vi } from "vitest";
import { dispatchGitHubWorkflow, ghHeaders } from "./github";
import type { Env } from "./types";

const env: Env = {
  DATABASE_URL: "postgres://unused",
  GITHUB_TOKEN: "test-token",
  GITHUB_CALLBACK_SECRET: "callback-secret",
  HASSAN_ENV: "test",
  HASSAN_VERSION: "0.5.0",
  GITHUB_REPO: "Hassankakaee333/hassan-cloud",
  GITHUB_WORKFLOW_FILE: "hassan-job.yml",
  ARTIFACT_BACKEND: "GITHUB_ACTIONS",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GitHub Actions integration", () => {
  it("uses the required GitHub API headers", () => {
    expect(ghHeaders("secret")).toMatchObject({
      Authorization: "Bearer secret",
      Accept: "application/vnd.github+json",
      "User-Agent": "HassanCloud-Worker/0.5",
    });
  });

  it("dispatches a workflow with Hassan job inputs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(dispatchGitHubWorkflow(env, "job-1", "project-1", "coding")).resolves.toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/actions/workflows/hassan-job.yml/dispatches");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      ref: "main",
      inputs: { job_id: "job-1", project_id: "project-1", job_type: "coding" },
    });
  });

  it("returns a bounded error when dispatch fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("denied", { status: 403 })));
    const result = await dispatchGitHubWorkflow(env, "job-1", "project-1", "coding");
    expect(result.ok).toBe(false);
    expect(result.error).toContain("GitHub dispatch 403");
  });
});
