import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_candidate_self_improve_job import _codex_quota_snapshot, _format_codex_quota


def test_daily_weekly_monthly_remaining_percentages():
    raw = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"usedPercent": 25, "windowDurationMins": 1440, "resetsAt": 1},
                "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 2},
            }
        },
        "individualLimit": {"remainingPercent": 33},
    }
    snapshot = _codex_quota_snapshot(raw)
    assert snapshot["limits"]["daily"]["remainingPercent"] == 75.0
    assert snapshot["limits"]["weekly"]["remainingPercent"] == 60.0
    assert snapshot["limits"]["monthly"]["remainingPercent"] == 33.0
    text = _format_codex_quota(snapshot)
    assert "اليومي 75%" in text
    assert "الأسبوعي 60%" in text
    assert "الشهري 33%" in text


def test_five_hour_is_shown_only_when_returned():
    raw = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10, "windowDurationMins": 300}}}
    text = _format_codex_quota(_codex_quota_snapshot(raw))
    assert "5 ساعات 90%" in text
    assert "اليومي" not in text
    assert "الشهري" not in text
