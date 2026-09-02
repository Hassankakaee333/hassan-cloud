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
  if (!listResp.ok) {
    console.warn("github_artifact_list_failed", { runId, status: listResp.status });
    return null;
  }
  const list = (await listResp.json()) as { artifacts?: Array<{ id: number; name: string }> };
  const artifact = list.artifacts?.find((a) => a.name === artifactName);
  if (!artifact) {
    console.warn("github_artifact_not_found", { runId, artifactName, count: list.artifacts?.length ?? 0 });
    return null;
  }
  const zipUrl = `https://api.github.com/repos/${owner}/${repo}/actions/artifacts/${artifact.id}/zip`;
  let zipResp = await fetch(zipUrl, {
    headers: GH_HEADERS(env.GITHUB_TOKEN),
    redirect: "manual",
  });
  if (zipResp.status >= 300 && zipResp.status < 400) {
    const signedUrl = zipResp.headers.get("Location");
    if (!signedUrl) {
      console.warn("github_artifact_redirect_missing", { runId, artifactId: artifact.id });
      return null;
    }
    // The signed blob URL authenticates itself. Forwarding the GitHub API
    // Authorization header to that different host causes a 401 response.
    zipResp = await fetch(signedUrl, { redirect: "follow" });
  }
  if (!zipResp.ok) {
    console.warn("github_artifact_download_failed", { runId, artifactId: artifact.id, status: zipResp.status });
    return null;
  }
  const zipBytes = await zipResp.arrayBuffer();
  const extracted = await extractFileFromZip(zipBytes, fileName);
  if (!extracted) {
    console.warn("github_artifact_file_not_found", {
      runId,
      artifactId: artifact.id,
      fileName,
      zipBytes: zipBytes.byteLength,
    });
  }
  return extracted;
}

export async function extractFileFromZip(zipBytes: ArrayBuffer, fileName: string): Promise<ArrayBuffer | null> {
  // GitHub artifact ZIPs use data descriptors, so sizes in local headers are
  // commonly zero. The central directory contains the authoritative sizes and
  // local-header offsets.
  const view = new DataView(zipBytes);
  const minimumEocdOffset = Math.max(0, zipBytes.byteLength - 0xffff - 22);
  let eocdOffset = -1;
  for (let offset = zipBytes.byteLength - 22; offset >= minimumEocdOffset; offset -= 1) {
    if (view.getUint32(offset, true) === 0x06054b50) {
      eocdOffset = offset;
      break;
    }
  }
  if (eocdOffset < 0) return null;

  const entryCount = view.getUint16(eocdOffset + 10, true);
  let offset = view.getUint32(eocdOffset + 16, true);
  for (let entry = 0; entry < entryCount; entry += 1) {
    if (offset + 46 > zipBytes.byteLength || view.getUint32(offset, true) !== 0x02014b50) return null;
    const compMethod = view.getUint16(offset + 10, true);
    const compSize = view.getUint32(offset + 20, true);
    const nameLen = view.getUint16(offset + 28, true);
    const extraLen = view.getUint16(offset + 30, true);
    const commentLen = view.getUint16(offset + 32, true);
    const localHeaderOffset = view.getUint32(offset + 42, true);
    const nameBytes = new Uint8Array(zipBytes, offset + 46, nameLen);
    const entryName = new TextDecoder().decode(nameBytes);
    if (entryName === fileName || entryName.endsWith(`/${fileName}`)) {
      if (
        localHeaderOffset + 30 > zipBytes.byteLength ||
        view.getUint32(localHeaderOffset, true) !== 0x04034b50
      ) {
        return null;
      }
      const localNameLen = view.getUint16(localHeaderOffset + 26, true);
      const localExtraLen = view.getUint16(localHeaderOffset + 28, true);
      const dataStart = localHeaderOffset + 30 + localNameLen + localExtraLen;
      if (dataStart + compSize > zipBytes.byteLength) return null;
      const compressed = new Uint8Array(zipBytes, dataStart, compSize);
      if (compMethod === 0) {
        return compressed.buffer.slice(compressed.byteOffset, compressed.byteOffset + compressed.byteLength);
      }
      if (compMethod === 8 && "DecompressionStream" in globalThis) {
        const ds = new DecompressionStream("deflate-raw");
        const stream = new Blob([compressed]).stream().pipeThrough(ds);
        return await new Response(stream).arrayBuffer();
      }
      return null;
    }
    offset += 46 + nameLen + extraLen + commentLen;
  }
  return null;
}
