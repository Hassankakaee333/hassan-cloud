from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_candidate_self_improve_job import _resolve_runtime_selection


CATALOG = [
    {
        "id": "picker-a",
        "model": "wire-a",
        "displayName": "Model A",
        "hidden": False,
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Low"},
            {"reasoningEffort": "high", "description": "High"},
        ],
    },
    {
        "id": "hidden-b",
        "model": "wire-b",
        "hidden": True,
        "supportedReasoningEfforts": ["medium"],
    },
]


def test_dynamic_selection_resolves_picker_id_to_wire_model():
    model, mode = _resolve_runtime_selection(CATALOG, "picker-a", "high")
    assert model == "wire-a"
    assert mode == "high"


def test_provider_default_mode_is_allowed_for_real_model():
    model, mode = _resolve_runtime_selection(CATALOG, "picker-a", None)
    assert model == "wire-a"
    assert mode is None


def test_removed_model_is_rejected_before_turn():
    try:
        _resolve_runtime_selection(CATALOG, "removed-model", "high")
    except RuntimeError as exc:
        assert "no longer available" in str(exc)
    else:
        raise AssertionError("removed model must be rejected")


def test_removed_reasoning_mode_is_rejected_before_turn():
    try:
        _resolve_runtime_selection(CATALOG, "picker-a", "xhigh")
    except RuntimeError as exc:
        assert "reasoning mode is no longer available" in str(exc)
    else:
        raise AssertionError("removed reasoning mode must be rejected")


def test_hidden_model_is_rejected():
    try:
        _resolve_runtime_selection(CATALOG, "hidden-b", "medium")
    except RuntimeError as exc:
        assert "hidden" in str(exc)
    else:
        raise AssertionError("hidden model must be rejected")
