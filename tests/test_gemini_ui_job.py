import sys
from pathlib import Path

import pytest

sys.path.insert(0, "scripts")

from gemini_ui_job import (
    GEMINI_FOREGROUND_PACKAGES,
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


def test_worker_uses_no_paid_provider_api_transport():
    source = Path("scripts/gemini_ui_job.py").read_text(encoding="utf-8").lower()
    assert "generativelanguage.googleapis.com" not in source
    assert "api.openai.com" not in source
    assert "api.deepseek.com" not in source
    assert "gemini_api_key" not in source


def test_workflow_routes_gemini_to_dedicated_worker_and_keeps_codex_manual():
    workflow = Path(".github/workflows/hassan-job.yml").read_text(encoding="utf-8")
    assert "inputs.job_type == 'gemini_ui_worker'" in workflow
    assert "python scripts/gemini_ui_job_runner.py" in workflow
    assert "Install Codex Python SDK only for explicit execution" in workflow
    assert "if: inputs.job_type == 'codex_candidate_self_improve'" in workflow
