import sys

import pytest

sys.path.insert(0, "scripts")

from gemini_ui_hardening import contains_secret_like, guard_no_secrets


def test_blocks_github_classic_pat_shape_without_using_real_secret():
    synthetic = "ghp_" + "A" * 36
    assert contains_secret_like({"token": synthetic})
    with pytest.raises(ValueError, match="credential-like material blocked"):
        guard_no_secrets({"token": synthetic}, where="test")


def test_blocks_fine_grained_github_pat_shape():
    synthetic = "github_pat_" + "B" * 40
    assert contains_secret_like(synthetic)


def test_blocks_private_key_header():
    assert contains_secret_like("-----BEGIN PRIVATE KEY-----")


def test_allows_normal_frishta_tool_payload():
    payload = {
        "tool": "phone.command",
        "arguments": {"action": "PING", "args": {}},
    }
    assert not contains_secret_like(payload)
    guard_no_secrets(payload, where="test")
