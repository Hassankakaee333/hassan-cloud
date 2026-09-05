"""Provider-neutral Frishta Tool Gateway.

Models never receive cloud/GitHub secrets. They request named tools; Hassan Cloud validates
scope, executes the tool, and returns a bounded result. The same contract can be used by
Gemini, DeepSeek, ChatGPT, or future providers.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_REPOS = {"Hassankakaee333/FMK-AI-BRIDGE", "Hassankakaee333/hassan-cloud"}
PHONE_REPO = "Hassankakaee333/FMK-AI-BRIDGE"
PHONE_REF = "phone-control"
MAX_TEXT_BYTES = 128 * 1024
PHONE_ACTIONS = {
    "PING", "UI_TREE", "OPEN_APP", "HOME", "BACK", "RECENTS", "NOTIFICATIONS",
    "QUICK_SETTINGS", "CLICK_TEXT", "SET_TEXT", "TAP", "SWIPE",
    "SCROLL_FORWARD", "SCROLL_BACKWARD", "SCREENSHOT",
}
FORBIDDEN_PHONE_ACTIONS = {"SET_SECRET_TEXT", "GET_SECURE_INPUT_KEY"}
STABLE_REFS = {"main", "master", "stable", "refs/heads/main", "refs/heads/master", "refs/heads/stable"}


@dataclass(frozen=True)
class ToolResult:
    status: str
    tool: str
    call_id: str
    data: Any = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"status": self.status, "tool": self.tool, "call_id": self.call_id}
        if self.data is not None:
            out["data"] = self.data
        if self.detail:
            out["detail"] = self.detail
        return out


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def tool_catalog() -> list[dict[str, Any]]:
    s = {"type": "string"}
    return [
        {"name": "cloud.projects.list", "description": "List Hassan Cloud projects.", "mutating": False, "input_schema": _schema({})},
        {"name": "cloud.jobs.list", "description": "List Hassan Cloud jobs/checkpoints.", "mutating": False, "input_schema": _schema({})},
        {"name": "cloud.job.get", "description": "Read one Hassan Cloud job.", "mutating": False, "input_schema": _schema({"job_id": s}, ["job_id"])},
        {"name": "cloud.artifacts.list", "description": "List Cloud artifacts.", "mutating": False, "input_schema": _schema({"project_id": s})},
        {"name": "github.file.read", "description": "Read a UTF-8 file from an allowed repository/ref.", "mutating": False, "input_schema": _schema({"repo": s, "ref": s, "path": s}, ["repo", "ref", "path"])},
        {"name": "github.branch.create_candidate", "description": "Create a new Candidate branch from another Candidate ref.", "mutating": True, "input_schema": _schema({"repo": s, "base_ref": s, "new_ref": s}, ["repo", "base_ref", "new_ref"])},
        {"name": "github.file.write_candidate", "description": "Create/update a UTF-8 file on a Candidate branch only.", "mutating": True, "input_schema": _schema({"repo": s, "ref": s, "path": s, "content": s, "message": s, "expected_sha": s}, ["repo", "ref", "path", "content", "message"])},
        {"name": "github.pr.open_candidate", "description": "Open a Candidate-to-Candidate pull request; Stable/main is forbidden.", "mutating": True, "input_schema": _schema({"repo": s, "head_ref": s, "base_ref": s, "title": s, "body": s}, ["repo", "head_ref", "base_ref", "title"])},
        {"name": "github.workflow.runs", "description": "Read recent workflow runs for a Candidate ref.", "mutating": False, "input_schema": _schema({"repo": s, "ref": s}, ["repo", "ref"])},
        {"name": "phone.command", "description": "Queue a non-secret Phone Agent command through the private phone-control branch.", "mutating": True, "input_schema": _schema({"action": s, "args": {"type": "object"}}, ["action"])},
        {"name": "phone.result", "description": "Poll the result of a previously queued Phone Agent command.", "mutating": False, "input_schema": _schema({"command_id": s}, ["command_id"])},
    ]


class ToolGateway:
    def __init__(self, repo, new_id, now_ms, *, client: httpx.Client | None = None) -> None:
        self.repo = repo
        self.new_id = new_id
        self.now_ms = now_ms
        self._client = client

    def _allowed_repos(self) -> set[str]:
        raw = os.environ.get("FRISHTA_TOOL_REPOS", "").strip()
        return {x.strip() for x in raw.split(",") if x.strip()} or set(DEFAULT_REPOS)

    def _check_repo(self, repo: str) -> None:
        if repo not in self._allowed_repos() or repo.count("/") != 1:
            raise ValueError("repository is not allowed")

    @staticmethod
    def _candidate_ref(ref: str) -> str:
        value = ref.strip()
        if value.lower() in STABLE_REFS or not value.startswith("frishta-"):
            raise ValueError("Candidate ref required; Stable/main is forbidden")
        return value

    def _token(self) -> str:
        token = os.environ.get("HASSAN_GITHUB_ACTIONS_TOKEN", "").strip()
        if not token:
            raise RuntimeError("HASSAN_GITHUB_ACTIONS_TOKEN is not configured")
        return token

    def _request(self, method: str, url: str, *, payload: dict[str, Any] | None = None) -> httpx.Response:
        own = self._client is None
        client = self._client or httpx.Client(timeout=30.0, trust_env=False)
        try:
            return client.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "Frishta-Tool-Gateway/1",
                },
                json=payload,
            )
        finally:
            if own:
                client.close()

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub HTTP {response.status_code}: {response.text[:300]}")
        if not response.content:
            return {}
        return response.json()

    def invoke(self, tool: str, args: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
        cid = call_id or self.new_id()
        try:
            data = self._invoke(tool, args or {})
            return ToolResult("OK", tool, cid, data=data).as_dict()
        except RuntimeError as exc:
            status = "NOT_CONFIGURED" if "not configured" in str(exc).lower() else "ERROR"
            return ToolResult(status, tool, cid, detail=str(exc)[:500]).as_dict()
        except (ValueError, KeyError, TypeError) as exc:
            return ToolResult("REJECTED", tool, cid, detail=str(exc)[:500]).as_dict()
        except Exception as exc:  # fail closed without exposing secrets
            return ToolResult("ERROR", tool, cid, detail=f"{type(exc).__name__}: {str(exc)[:300]}").as_dict()

    def _invoke(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "cloud.projects.list":
            return self.repo.list_projects()
        if tool == "cloud.jobs.list":
            return self.repo.list_jobs()
        if tool == "cloud.job.get":
            row = self.repo.get_job(str(args["job_id"]))
            if not row:
                raise ValueError("job not found")
            return row
        if tool == "cloud.artifacts.list":
            return self.repo.list_artifacts(project_id=args.get("project_id"))
        if tool == "github.file.read":
            return self._github_read(str(args["repo"]), str(args["ref"]), str(args["path"]))
        if tool == "github.branch.create_candidate":
            return self._github_create_branch(str(args["repo"]), str(args["base_ref"]), str(args["new_ref"]))
        if tool == "github.file.write_candidate":
            return self._github_write(args)
        if tool == "github.pr.open_candidate":
            return self._github_open_pr(args)
        if tool == "github.workflow.runs":
            return self._github_runs(str(args["repo"]), str(args["ref"]))
        if tool == "phone.command":
            return self._phone_command(str(args["action"]), dict(args.get("args") or {}))
        if tool == "phone.result":
            return self._phone_result(str(args["command_id"]))
        raise ValueError("unknown tool")

    def _github_read(self, repo: str, ref: str, path: str) -> dict[str, Any]:
        self._check_repo(repo)
        if not ref.strip() or not path.strip() or ".." in path.split("/"):
            raise ValueError("invalid ref/path")
        url = f"https://api.github.com/repos/{repo}/contents/{quote(path.strip(), safe='/')}?ref={quote(ref.strip(), safe='')}"
        row = self._json(self._request("GET", url))
        if row.get("type") != "file" or row.get("encoding") != "base64":
            raise ValueError("UTF-8 file required")
        raw = base64.b64decode(row.get("content", ""), validate=False)
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError("file exceeds tool read limit")
        return {"path": path, "ref": ref, "sha": row.get("sha"), "content": raw.decode("utf-8")}

    def _github_create_branch(self, repo: str, base_ref: str, new_ref: str) -> dict[str, Any]:
        self._check_repo(repo)
        base = self._candidate_ref(base_ref)
        new = self._candidate_ref(new_ref)
        base_row = self._json(self._request("GET", f"https://api.github.com/repos/{repo}/git/ref/heads/{quote(base, safe='')}"))
        sha = base_row["object"]["sha"]
        row = self._json(self._request("POST", f"https://api.github.com/repos/{repo}/git/refs", payload={"ref": f"refs/heads/{new}", "sha": sha}))
        return {"ref": new, "sha": row.get("object", {}).get("sha", sha), "base_ref": base}

    def _github_write(self, args: dict[str, Any]) -> dict[str, Any]:
        repo = str(args["repo"]); self._check_repo(repo)
        ref = self._candidate_ref(str(args["ref"]))
        path = str(args["path"]).strip(); content = str(args["content"])
        if not path or ".." in path.split("/") or len(content.encode()) > MAX_TEXT_BYTES:
            raise ValueError("invalid path/content")
        payload: dict[str, Any] = {"message": str(args["message"])[:200], "content": base64.b64encode(content.encode()).decode(), "branch": ref}
        expected = str(args.get("expected_sha") or "").strip()
        if expected:
            payload["sha"] = expected
        url = f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}"
        row = self._json(self._request("PUT", url, payload=payload))
        return {"ref": ref, "path": path, "commit_sha": row.get("commit", {}).get("sha"), "content_sha": row.get("content", {}).get("sha")}

    def _github_open_pr(self, args: dict[str, Any]) -> dict[str, Any]:
        repo = str(args["repo"]); self._check_repo(repo)
        head = self._candidate_ref(str(args["head_ref"])); base = self._candidate_ref(str(args["base_ref"]))
        payload = {"title": str(args["title"])[:200], "body": str(args.get("body") or "")[:10000], "head": head, "base": base, "draft": False}
        row = self._json(self._request("POST", f"https://api.github.com/repos/{repo}/pulls", payload=payload))
        return {"number": row.get("number"), "url": row.get("html_url"), "head_ref": head, "base_ref": base}

    def _github_runs(self, repo: str, ref: str) -> dict[str, Any]:
        self._check_repo(repo); candidate = self._candidate_ref(ref)
        row = self._json(self._request("GET", f"https://api.github.com/repos/{repo}/actions/runs?branch={quote(candidate, safe='')}&per_page=10"))
        runs = [{"id": r.get("id"), "name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion"), "head_sha": r.get("head_sha"), "url": r.get("html_url")} for r in row.get("workflow_runs", [])]
        return {"ref": candidate, "runs": runs}

    def _phone_command(self, action: str, action_args: dict[str, Any]) -> dict[str, Any]:
        upper = action.strip().upper()
        if upper in FORBIDDEN_PHONE_ACTIONS or upper not in PHONE_ACTIONS:
            raise ValueError("phone action is not allowed by Tool Gateway")
        cid = self.new_id()
        command = {"id": cid, "action": upper, "requiresConfirmation": False, "expiresAtEpochMs": self.now_ms() + 120_000}
        for key, value in action_args.items():
            if key in {"id", "action", "requiresConfirmation", "expiresAtEpochMs"}:
                continue
            command[key] = value
        data = json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode()
        payload = {"message": f"Frishta Tool Gateway phone command {cid}", "content": base64.b64encode(data).decode(), "branch": PHONE_REF}
        url = f"https://api.github.com/repos/{PHONE_REPO}/contents/inbox/{cid}.json"
        self._json(self._request("PUT", url, payload=payload))
        return {"command_id": cid, "status": "QUEUED", "action": upper}

    def _phone_result(self, command_id: str) -> dict[str, Any]:
        cid = command_id.strip()
        if not cid or "/" in cid or ".." in cid:
            raise ValueError("invalid command_id")
        url = f"https://api.github.com/repos/{PHONE_REPO}/contents/outbox/{quote(cid, safe='')}.json?ref={PHONE_REF}"
        response = self._request("GET", url)
        if response.status_code == 404:
            return {"command_id": cid, "status": "PENDING"}
        row = self._json(response)
        raw = base64.b64decode(row.get("content", ""), validate=False)
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError("phone result too large")
        return json.loads(raw.decode("utf-8"))
