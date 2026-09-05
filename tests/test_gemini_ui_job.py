import sys
from pathlib import Path

import pytest

sys.path.insert(0, "scripts")

import gemini_ui_atomic_transport as atomic_transport
from gemini_ui_job import (
    GEMINI_FOREGROUND_PACKAGES,
    PHONE_ACTIONS,
    _extract_protocol,
    _safe_candidate_ref,
    tool_catalog,
)


def test_gemini_current_android_foreground_package_is_supported():
    assert "com.google.android.googlequicksearchbox" in GEMINI_FOREGROUND_PACKAGES
    assert "com.google.android.apps.bard" in GEMINI_FOREGROUND_PACKAGES


def test_protocol_extracts_tool_and_final_markers():
    tool_tree = '1|pkg|TextView|text=FRISHTA_TOOL:{"tool":"cloud.job.context","arguments":{}}|desc=|bounds=1 2 3 4|clickable=false|editable=false|password=false'
    final_tree = '1|pkg|TextView|text=FRISHTA_FINAL:{"summary":"done"}|desc=|bounds=1 2 3 4|clickable=false|editable=false|password=false'
    kind, payload = _extract_protocol(tool_tree)
    assert kind == "tool"
    assert '"cloud.job.context"' in payload
    kind, payload = _extract_protocol(final_tree)
    assert kind == "final"
    assert '"done"' in payload


def test_stable_refs_are_blocked_for_candidate_writes():
    for ref in ("main", "master", "stable", "refs/heads/main"):
        with pytest.raises(ValueError):
            _safe_candidate_ref(ref)
    assert _safe_candidate_ref("frishta-gemini-test") == "frishta-gemini-test"


def test_catalog_has_shared_phone_cloud_github_tools_without_codex():
    names = {item["name"] for item in tool_catalog()}
    assert "cloud.job.context" in names
    assert "cloud.workspace.list" in names
    assert "github.file.write_candidate" in names
    assert "phone.command" in names
    assert not any("codex" in name.lower() for name in names)


def test_atomic_gemini_exchange_is_transport_private_not_public_phone_action():
    assert atomic_transport.INTERNAL_GEMINI_ACTION == "GEMINI_EXCHANGE"
    assert "GEMINI_EXCHANGE" not in PHONE_ACTIONS
    catalog_text = "\n".join(item["name"] + " " + item["description"] for item in tool_catalog())
    assert "GEMINI_EXCHANGE" not in catalog_text


def test_atomic_transport_binds_nonce_and_expected_marker(monkeypatch):
    calls = []

    def fake_exchange(text, *, expected_marker, nonce, timeout):
        calls.append((text, expected_marker, nonce, timeout))
        if expected_marker == atomic_transport.TOOL_MARKER:
            return f'FRISHTA_TOOL:{{"tool":"phone.command","arguments":{{"action":"PING"}},"nonce":"{nonce}"}}'
        return f'FRISHTA_FINAL:{{"summary":"gemini-tool-gateway-ok","nonce":"{nonce}"}}'

    monkeypatch.setattr(atomic_transport, "_internal_gemini_exchange", fake_exchange)

    class DummyTransport:
        pass

    transport = DummyTransport()
    atomic_transport._atomic_send(transport, "initial")
    assert calls[-1][1] == atomic_transport.TOOL_MARKER
    assert "FRISHTA_NONCE=s-" in calls[-1][0]
    kind, payload = atomic_transport._atomic_await_protocol(transport)
    assert kind == "tool"
    assert '"PING"' in payload

    atomic_transport._atomic_send(transport, "TOOL_RESULT={\"status\":\"OK\"}")
    assert calls[-1][1] == atomic_transport.FINAL_MARKER
    kind, payload = atomic_transport._atomic_await_protocol(transport)
    assert kind == "final"
    assert "gemini-tool-gateway-ok" in payload


def test_phone_command_put_retries_github_409_branch_races(monkeypatch):
    calls = []

    def flaky_put(path, payload, message):
        calls.append((path, payload, message))
        if len(calls) < 3:
            raise RuntimeError('GitHub HTTP 409: branch moved')

    monkeypatch.setattr(atomic_transport, "_raw_put_phone_file", flaky_put)
    monkeypatch.setattr(atomic_transport.time, "sleep", lambda _seconds: None)
    atomic_transport._put_with_retry("inbox/test.json", {"id": "test"}, "test")
    assert len(calls) == 3


def test_worker_uses_no_paid_provider_api_transport():
    source = (
        Path("scripts/gemini_ui_job.py").read_text(encoding="utf-8")
        + Path("scripts/gemini_ui_atomic_transport.py").read_text(encoding="utf-8")
    ).lower()
    assert "generativelanguage.googleapis.com" not in source
    assert "api.openai.com" not in source
    assert "api.deepseek.com" not in source
    assert "gemini_api_key" not in source


def test_workflow_routes_gemini_to_dedicated_worker_and_keeps_codex_manual():
    workflow = Path(".github/workflows/hassan-job.yml").read_text(encoding="utf-8")
    runner = Path("scripts/gemini_ui_job_runner.py").read_text(encoding="utf-8")
    assert "inputs.job_type == 'gemini_ui_worker'" in workflow
    assert "python scripts/gemini_ui_job_runner.py" in workflow
    assert "Install Codex Python SDK only for explicit execution" in workflow
    assert "if: inputs.job_type == 'codex_candidate_self_improve'" in workflow
    assert "install_runtime_hardening()" in runner
    assert "install_atomic_transport()" in runner
