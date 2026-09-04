from __future__ import annotations

from pathlib import Path


def test_shadow_workflow_keeps_callback_secrets_out_of_untrusted_job() -> None:
    workflow = Path(".github/workflows/agent-acp-shadow.yml").read_text(encoding="utf-8")
    shadow_job = workflow.split("  publish:", 1)[0]
    publish_job = workflow.split("  publish:", 1)[1]

    assert 'HASSAN_CALLBACK_SECRET: ""' in shadow_job
    assert 'HASSAN_API_URL: ""' in shadow_job
    assert "secrets.HASSAN_CALLBACK_SECRET" not in shadow_job
    assert "secrets.HASSAN_API_URL" not in shadow_job
    assert "session/prompt" not in shadow_job
    assert "prompt_sent" in shadow_job
    assert "auth_attempted" in shadow_job
    assert "permission_requests" in shadow_job
    assert "tool_requests" in shadow_job

    assert "secrets.HASSAN_CALLBACK_SECRET" in publish_job
    assert "secrets.HASSAN_API_URL" in publish_job
    assert "needs: shadow" in publish_job
