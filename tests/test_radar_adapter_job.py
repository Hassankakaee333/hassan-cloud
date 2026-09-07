from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from radar_adapter_job import (  # noqa: E402
    PLAYWRIGHT_ADAPTER_ID,
    PLAYWRIGHT_REPOSITORY,
    PLAYWRIGHT_VERSION,
    parse_adapter_request,
    sanitized_child_env,
)


def test_playwright_adapter_is_pinned_to_reviewed_identity():
    assert PLAYWRIGHT_ADAPTER_ID == "playwright-browser-ci-v1"
    assert PLAYWRIGHT_VERSION == "1.63.0"
    assert PLAYWRIGHT_REPOSITORY == "https://github.com/microsoft/playwright"


def test_adapter_request_requires_structured_whitelisted_smoke():
    request = parse_adapter_request(
        json.dumps({"adapter_id": "playwright-browser-ci-v1", "action": "smoke"})
    )
    assert request == {"adapter_id": "playwright-browser-ci-v1", "action": "smoke"}

    with pytest.raises(ValueError, match="JSON object"):
        parse_adapter_request("playwright-browser-ci-v1")
    with pytest.raises(ValueError, match="unsupported radar adapter"):
        parse_adapter_request(json.dumps({"adapter_id": "random-browser-agent", "action": "smoke"}))
    with pytest.raises(ValueError, match="unsupported radar adapter action"):
        parse_adapter_request(json.dumps({"adapter_id": "playwright-browser-ci-v1", "action": "browse-internet"}))


def test_child_environment_removes_credentials_and_hassan_control_secrets():
    clean = sanitized_child_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/runner",
            "CI": "true",
            "LANG": "C.UTF-8",
            "HASSAN_CALLBACK_SECRET": "do-not-pass",
            "HASSAN_API_URL": "https://secret-control.example",
            "GEMINI_API_KEY": "do-not-pass",
            "OPENAI_API_KEY": "do-not-pass",
            "GITHUB_TOKEN": "do-not-pass",
            "CODEX_SESSION_CACHE": "do-not-pass",
            "AWS_SECRET_ACCESS_KEY": "do-not-pass",
            "NPM_CONFIG_CACHE": "/tmp/npm-cache",
        }
    )
    assert clean["PATH"] == "/usr/bin"
    assert clean["HOME"] == "/home/runner"
    assert clean["CI"] == "true"
    assert clean["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    assert clean["FRISHTA_RADAR_ADAPTER"] == "playwright-browser-ci-v1"
    assert clean["NPM_CONFIG_CACHE"] == "/tmp/npm-cache"
    assert all("SECRET" not in key for key in clean)
    assert all("TOKEN" not in key for key in clean)
    assert all("API_KEY" not in key for key in clean)
    assert all(not key.startswith("HASSAN_") for key in clean if key != "FRISHTA_RADAR_ADAPTER")
    assert "GEMINI_API_KEY" not in clean
    assert "GITHUB_TOKEN" not in clean
