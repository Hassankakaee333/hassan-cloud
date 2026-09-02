import { afterEach, describe, expect, it, vi } from "vitest";
import { dispatchGitHubWorkflow, downloadGitHubArtifactFile, extractFileFromZip, ghHeaders } from "./github";
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

  it("extracts GitHub-style ZIP entries whose local sizes use a data descriptor", async () => {
    const fileName = "artifacts/workspace.zip";
    const payload = new TextEncoder().encode("durable artifact");
    const zip = buildStoredZipWithDescriptor(fileName, payload);

    const extracted = await extractFileFromZip(zip, fileName);

    expect(extracted).not.toBeNull();
    expect(new TextDecoder().decode(extracted!)).toBe("durable artifact");
    await expect(extractFileFromZip(zip, "artifacts/missing.zip")).resolves.toBeNull();
  });

  it("downloads a signed artifact redirect without forwarding the GitHub token", async () => {
    const fileName = "artifacts/workspace.zip";
    const zip = buildStoredZipWithDescriptor(fileName, new TextEncoder().encode("from signed storage"));
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({ artifacts: [{ id: 99, name: "hassan-job-job-1" }] }),
      )
      .mockResolvedValueOnce(
        new Response(null, { status: 302, headers: { Location: "https://signed.example/artifact.zip" } }),
      )
      .mockResolvedValueOnce(new Response(zip, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const extracted = await downloadGitHubArtifactFile(
      env,
      "run-1",
      "hassan-job-job-1",
      fileName,
    );

    expect(new TextDecoder().decode(extracted!)).toBe("from signed storage");
    expect(fetchMock.mock.calls[2][0]).toBe("https://signed.example/artifact.zip");
    expect(fetchMock.mock.calls[2][1]).toEqual({ redirect: "follow" });
  });
});

function buildStoredZipWithDescriptor(fileName: string, payload: Uint8Array): ArrayBuffer {
  const name = new TextEncoder().encode(fileName);
  const local = new Uint8Array(30 + name.length + payload.length + 16);
  const localView = new DataView(local.buffer);
  localView.setUint32(0, 0x04034b50, true);
  localView.setUint16(4, 20, true);
  localView.setUint16(6, 0x08, true);
  localView.setUint16(8, 0, true);
  localView.setUint16(26, name.length, true);
  local.set(name, 30);
  local.set(payload, 30 + name.length);
  const descriptorOffset = 30 + name.length + payload.length;
  localView.setUint32(descriptorOffset, 0x08074b50, true);
  localView.setUint32(descriptorOffset + 8, payload.length, true);
  localView.setUint32(descriptorOffset + 12, payload.length, true);

  const central = new Uint8Array(46 + name.length);
  const centralView = new DataView(central.buffer);
  centralView.setUint32(0, 0x02014b50, true);
  centralView.setUint16(4, 20, true);
  centralView.setUint16(6, 20, true);
  centralView.setUint16(8, 0x08, true);
  centralView.setUint16(10, 0, true);
  centralView.setUint32(20, payload.length, true);
  centralView.setUint32(24, payload.length, true);
  centralView.setUint16(28, name.length, true);
  central.set(name, 46);

  const eocd = new Uint8Array(22);
  const eocdView = new DataView(eocd.buffer);
  eocdView.setUint32(0, 0x06054b50, true);
  eocdView.setUint16(8, 1, true);
  eocdView.setUint16(10, 1, true);
  eocdView.setUint32(12, central.length, true);
  eocdView.setUint32(16, local.length, true);

  const zip = new Uint8Array(local.length + central.length + eocd.length);
  zip.set(local, 0);
  zip.set(central, local.length);
  zip.set(eocd, local.length + central.length);
  return zip.buffer;
}
