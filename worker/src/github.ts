import type { Env } from "./types";

const GH_HEADERS = (token: string) => ({
  Authorization: `Bearer ${token}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "HassanCloud-Worker/0.5",
});

export const ghHeaders = GH_HEADERS;

export async function dispatchGitHubWorkflow(
  env: Env,
  jobId: string,
  projectId: string,
  jobType: string,
): Promise<{ ok: boolean; runId?: string; error?: string }> {
  const [owner, repo] = env.GITHUB_REPO.split("/");
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${env.GITHUB_WORKFLOW_FILE}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      ...GH_HEADERS(env.GITHUB_TOKEN),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      ref: "main",
      inputs: { job_id: jobId, project_id: projectId, job_type: jobType },
    }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    return { ok: false, error: `GitHub dispatch ${resp.status}: ${text.slice(0, 300)}` };
  }
  return { ok: true };
}

async function findLatestRunId(env: Env, jobId: string): Promise<string | null> {
  const [owner, repo] = env.GITHUB_REPO.split("/");
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${env.GITHUB_WORKFLOW_FILE}/runs?per_page=5`;
  const resp = await fetch(url, {
    headers: GH_HEADERS(env.GITHUB_TOKEN),
  });
  if (!resp.ok) return null;
  const data = (await resp.json()) as { workflow_runs?: Array<{ id: number; display_title?: string }> };
  const match = data.workflow_runs?.find((r) => String(r.display_title ?? "").includes(jobId));
  return match ? String(match.id) : data.workflow_runs?.[0] ? String(data.workflow_runs[0].id) : null;
}

export async function cancelGitHubRun(env: Env, runId: string): Promise<boolean> {
  const [owner, repo] = env.GITHUB_REPO.split("/");
  const url = `https://api.github.com/repos/${owner}/${repo}/actions/runs/${runId}/cancel`;
  const resp = await fetch(url, {
    method: "POST",
    headers: GH_HEADERS(env.GITHUB_TOKEN),
  });
  return resp.ok;
}

export async function downloadGitHubArtifactFile(
  env: Env,
  runId: string,
  artifactName: string,
  fileName: string,
): Promise<ArrayBuffer | null> {
  const [owner, repo] = env.GITHUB_REPO.split("/");
  const listUrl = `https://api.github.com/repos/${owner}/${repo}/actions/runs/${runId}/artifacts?per_page=20`;
  const listResp = await fetch(listUrl, {
    headers: GH_HEADERS(env.GITHUB_TOKEN),
  });
  if (!listResp.ok) return null;
  const list = (await listResp.json()) as { artifacts?: Array<{ id: number; name: string }> };
  const artifact = list.artifacts?.find((a) => a.name === artifactName);
  if (!artifact) return null;
  const zipUrl = `https://api.github.com/repos/${owner}/${repo}/actions/artifacts/${artifact.id}/zip`;
  const zipResp = await fetch(zipUrl, {
    headers: GH_HEADERS(env.GITHUB_TOKEN),
  });
  if (!zipResp.ok) return null;
  const zipBytes = await zipResp.arrayBuffer();
  return extractFileFromZip(zipBytes, fileName);
}

async function extractFileFromZip(zipBytes: ArrayBuffer, fileName: string): Promise<ArrayBuffer | null> {
  // Minimal ZIP local-header scan for POC (single small files in artifact)
  const view = new DataView(zipBytes);
  let offset = 0;
  while (offset + 30 < zipBytes.byteLength) {
    const sig = view.getUint32(offset, true);
    if (sig !== 0x04034b50) break;
    const compMethod = view.getUint16(offset + 8, true);
    const compSize = view.getUint32(offset + 18, true);
    const nameLen = view.getUint16(offset + 26, true);
    const extraLen = view.getUint16(offset + 28, true);
    const nameBytes = new Uint8Array(zipBytes, offset + 30, nameLen);
    const entryName = new TextDecoder().decode(nameBytes);
    const dataStart = offset + 30 + nameLen + extraLen;
    const dataEnd = dataStart + compSize;
    if (entryName === fileName || entryName.endsWith(`/${fileName}`)) {
      const compressed = new Uint8Array(zipBytes, dataStart, compSize);
      if (compMethod === 0) {
        return compressed.buffer.slice(compressed.byteOffset, compressed.byteOffset + compressed.byteLength);
      }
      if (compMethod === 8 && "DecompressionStream" in globalThis) {
        const ds = new DecompressionStream("deflate-raw");
        const stream = new Blob([compressed]).stream().pipeThrough(ds);
        return await new Response(stream).arrayBuffer();
      }
    }
    offset = dataEnd;
  }
  return null;
}
