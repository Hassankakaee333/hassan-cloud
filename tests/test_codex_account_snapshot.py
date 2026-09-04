from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from codex_account_snapshot_job import _parse_responses, _safe_account, _safe_models, _safe_rate_limits


def test_parse_app_server_responses_ignores_noise():
    raw = '\n'.join([
        'not-json',
        '{"id":2,"result":{"account":{"type":"chatgpt"}}}',
        '{"method":"notification/example","params":{}}',
        '{"id":3,"result":{"data":[]}}',
    ])
    parsed = _parse_responses(raw)
    assert sorted(parsed) == [2, 3]


def test_account_snapshot_keeps_identity_but_not_tokens():
    safe = _safe_account({
        "account": {
            "type": "chatgpt",
            "email": "hassan@example.com",
            "planType": "plus",
            "accessToken": "SECRET",
            "refreshToken": "SECRET2",
        },
        "requiresOpenaiAuth": True,
    })
    assert safe["type"] == "chatgpt"
    assert safe["planType"] == "plus"
    assert "accessToken" not in safe
    assert "refreshToken" not in safe


def test_models_preserve_server_order_and_reasoning_options():
    models = _safe_models({"data": [
        {
            "id": "model-a",
            "displayName": "Model A",
            "supportedReasoningEfforts": ["low", "medium", "high"],
            "secretField": "drop-me",
        },
        {
            "id": "model-b",
            "supportedReasoningEfforts": [{"reasoningEffort":"medium","description":"Balanced"}],
        },
    ]})
    assert [item["id"] for item in models] == ["model-a", "model-b"]
    assert models[0]["supportedReasoningEfforts"] == ["low", "medium", "high"]
    assert "secretField" not in models[0]


def test_rate_limits_classify_by_duration_not_slot_name():
    safe = _safe_rate_limits({
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"usedPercent": 31, "windowDurationMins": 10080, "resetsAt": 123},
                "secondary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 456},
                "planType": "plus",
            }
        }
    })
    assert safe["limits"]["weekly"]["remainingPercent"] == 69.0
    assert safe["limits"]["five_hours"]["remainingPercent"] == 90.0
    assert safe["planType"] == "plus"
