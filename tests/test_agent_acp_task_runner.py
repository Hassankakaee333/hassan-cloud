from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agent_acp_task_runner as runner  # noqa: E402


class FakeProcess:
    def __init__(self, frames: list[dict]) -> None:
        raw = b"".join(
            (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
            for frame in frames
        )
        self.stdout = io.BytesIO(raw)


def bundle() -> dict:
    data = b"approved file contents\n"
    digest = hashlib.sha256(data).hexdigest()
    value = {
        "permit_id": "agent-task-permit-sha256:" + "0" * 64,
        "execution_request_id": "agent-exec-000000000001",
        "approval_nonce": "approval_nonce_000000000001",
        "approval_evidence_id": "approval-1",
        "comparison_evidence_id": "compare-1",
        "task_id": "task-1",
        "goal": "Read the approved file and summarize it.",
        "files": [
            {
                "path": "docs/input.txt",
                "sha256": digest,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
        ],
        "actions": ["READ_FILES"],
        "targets_stable_directly": False,
        "cost_class": "FREE",
        "additional_spend_cents": 0,
        "agent_id": "sample-agent",
        "version": "1.2.3",
        "protocol_version": 1,
    }
    files = runner._decode_files(value)
    value["goal_sha256"] = hashlib.sha256(value["goal"].encode("utf-8")).hexdigest()
    value["permit_id"] = runner._expected_permit_id(value, files)
    return value


def test_bundle_binds_goal_and_file_bytes_to_permit() -> None:
    value = bundle()
    files = runner.validate_bundle(value)
    assert files[0][0] == "docs/input.txt"

    changed_goal = dict(value, goal="different")
    with pytest.raises(ValueError, match="goal SHA-256 mismatch"):
        runner.validate_bundle(changed_goal)

    changed_file = json.loads(json.dumps(value))
    changed_file["files"][0]["content_base64"] = base64.b64encode(b"different").decode("ascii")
    with pytest.raises(ValueError, match="file content SHA-256 mismatch"):
        runner.validate_bundle(changed_file)


def test_session_new_uses_read_only_workspace_and_prompt_is_one_text_block() -> None:
    session_new = json.loads(runner._task_session_new_request())
    assert session_new["method"] == "session/new"
    assert session_new["params"] == {"cwd": "/workspace", "mcpServers": []}

    prompt = json.loads(runner._prompt_request("session-1", "approved goal"))
    assert prompt["method"] == "session/prompt"
    assert prompt["params"]["sessionId"] == "session-1"
    assert prompt["params"]["prompt"] == [{"type": "text", "text": "approved goal"}]


def test_prompt_turn_accepts_session_updates_then_final_response() -> None:
    process = FakeProcess(
        [
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": "s1", "update": {"kind": "agent_message_chunk", "text": "hi"}},
            },
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
        ]
    )
    state = {"agent_client_requests": 0}
    final_raw, updates, stop_reason = runner._read_prompt_turn(process, 5, state)
    assert json.loads(final_raw)["id"] == 3
    assert "session/update" in updates
    assert stop_reason == "end_turn"
    assert state["agent_client_requests"] == 0


def test_prompt_turn_never_services_agent_to_client_request() -> None:
    process = FakeProcess(
        [
            {
                "jsonrpc": "2.0",
                "id": 91,
                "method": "session/request_permission",
                "params": {"sessionId": "s1"},
            }
        ]
    )
    state = {"agent_client_requests": 0}
    with pytest.raises(ValueError, match="Agent->Client"):
        runner._read_prompt_turn(process, 5, state)
    assert state["agent_client_requests"] == 1


def test_truth_state_starts_false_until_real_process_and_prompt_steps() -> None:
    report = runner._base_report(bundle())
    assert report["artifact_executed"] is False
    assert report["session_created"] is False
    assert report["prompt_sent"] is False
    assert report["agent_client_requests"] == 0
