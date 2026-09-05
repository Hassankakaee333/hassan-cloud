from __future__ import annotations

import os

import pytest

from hassan_cloud.tool_gateway import PHONE_ACTIONS, ToolGateway, tool_catalog


class FakeRepo:
    def list_projects(self):
        return [{"id": "p1"}]

    def list_jobs(self):
        return [{"id": "j1", "state": "QUEUED"}]

    def get_job(self, job_id):
        return {"id": job_id, "state": "COMPLETED"} if job_id == "j1" else None

    def list_artifacts(self, project_id=None):
        return [{"id": "a1", "project_id": project_id}]


def gateway():
    return ToolGateway(FakeRepo(), lambda: "id-1", lambda: 1000)


def test_catalog_is_provider_neutral_and_exposes_no_secret_tools():
    tools = {item["name"]: item for item in tool_catalog()}
    assert "phone.command" in tools
    assert "github.file.write_candidate" in tools
    assert "github.pr.open_candidate" in tools
    assert "cloud.jobs.list" in tools
    joined = " ".join(tools)
    assert "secret" not in joined.lower()
    assert "credential" not in joined.lower()


def test_cloud_tools_work_without_provider_or_github_credentials():
    g = gateway()
    assert g.invoke("cloud.projects.list", {})["status"] == "OK"
    assert g.invoke("cloud.jobs.list", {})["data"][0]["id"] == "j1"
    assert g.invoke("cloud.job.get", {"job_id": "missing"})["status"] == "REJECTED"


def test_stable_and_non_candidate_writes_are_rejected():
    g = gateway()
    for ref in ("main", "master", "stable", "feature-x", "refs/heads/main"):
        result = g.invoke(
            "github.file.write_candidate",
            {"repo": "Hassankakaee333/FMK-AI-BRIDGE", "ref": ref, "path": "x.txt", "content": "x", "message": "x"},
        )
        assert result["status"] == "REJECTED"


def test_phone_secret_input_actions_are_never_available():
    assert "SET_SECRET_TEXT" not in PHONE_ACTIONS
    assert "GET_SECURE_INPUT_KEY" not in PHONE_ACTIONS
    g = gateway()
    assert g.invoke("phone.command", {"action": "SET_SECRET_TEXT", "args": {}})["status"] == "REJECTED"


def test_github_tools_fail_closed_when_server_token_missing(monkeypatch):
    monkeypatch.delenv("HASSAN_GITHUB_ACTIONS_TOKEN", raising=False)
    g = gateway()
    result = g.invoke(
        "github.file.read",
        {"repo": "Hassankakaee333/FMK-AI-BRIDGE", "ref": "frishta-test", "path": "README.md"},
    )
    assert result["status"] == "NOT_CONFIGURED"
    assert "token" not in str(result).lower() or "not configured" in str(result).lower()


def test_allowed_phone_action_still_requires_server_side_github_channel(monkeypatch):
    monkeypatch.delenv("HASSAN_GITHUB_ACTIONS_TOKEN", raising=False)
    g = gateway()
    result = g.invoke("phone.command", {"action": "UI_TREE", "args": {}})
    assert result["status"] == "NOT_CONFIGURED"
