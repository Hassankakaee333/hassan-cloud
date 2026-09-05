from __future__ import annotations

from pathlib import Path


def test_agent_task_workflow_keeps_prompt_out_of_dispatch_and_secrets_out_of_execute() -> None:
    path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "agent-acp-task.yml"
    text = path.read_text(encoding="utf-8")

    dispatch_section = text.split("permissions:", 1)[0]
    assert "job_id:" in dispatch_section
    assert "goal:" not in dispatch_section
    assert "permit_id:" not in dispatch_section
    assert "files:" not in dispatch_section
    assert "prompt:" not in dispatch_section

    execute = text.split("  execute:", 1)[1].split("  publish:", 1)[0]
    assert 'GITHUB_TOKEN: ""' in execute
    assert 'GH_TOKEN: ""' in execute
    assert 'HASSAN_CALLBACK_SECRET: ""' in execute
    assert 'HASSAN_API_URL: ""' in execute
    assert 'OPENAI_API_KEY: ""' in execute
    assert "secrets.HASSAN_CALLBACK_SECRET" not in execute
    assert "secrets.HASSAN_API_URL" not in execute
    assert "agent_acp_task_runner.py" in execute
    assert "persist-credentials: false" in execute

    prepare = text.split("  prepare:", 1)[1].split("  execute:", 1)[0]
    assert "secrets.HASSAN_CALLBACK_SECRET" in prepare
    assert "/v1/internal/agent-executions/{job_id}/bundle" in prepare
    assert "retention-days: 1" in prepare

    publish = text.split("  publish:", 1)[1]
    assert "secrets.HASSAN_CALLBACK_SECRET" in publish
    assert "/v1/internal/agent-executions/{job_id}/evidence" in publish
    assert "frishta-agent-task-bundle" not in publish


def test_runner_source_enforces_read_only_no_network_container_boundary() -> None:
    path = Path(__file__).resolve().parents[1] / "scripts" / "agent_acp_task_runner.py"
    text = path.read_text(encoding="utf-8")
    for required in (
        '"--network", "none"',
        '"--read-only"',
        '"--cap-drop", "ALL"',
        '"no-new-privileges:true"',
        '"--user", "65534:65534"',
        ':/workspace:ro',
        '"--workdir", "/workspace"',
        'actions != ["READ_FILES"]',
        'Agent->Client request/notification forbidden',
    ):
        assert required in text, required
