"""Unit tests for self-improve code apply helpers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_self_improve_job import apply_code_ops, _safe_rel  # noqa: E402


def test_safe_rel_rejects_traversal():
    assert _safe_rel("../secrets.txt") is None
    assert _safe_rel("C:/Windows/x.kt") is None
    assert _safe_rel("app/src/main/java/Foo.kt") == "app/src/main/java/Foo.kt"


def test_apply_write_and_replace(tmp_path: Path):
    root = tmp_path
    (root / "app/src/main/java").mkdir(parents=True)
    target = root / "app/src/main/java/Demo.kt"
    target.write_text("fun a() = 1\n", encoding="utf-8")
    payload = {
        "files": [
            {"path": "app/src/main/java/Demo.kt", "action": "replace", "old": "1", "new": "2"},
            {
                "path": "docs/NOTE.md",
                "action": "write",
                "content": "# hi\n",
            },
            {"path": "app/src/main/java/Demo.kt", "action": "replace", "old": "NOPE", "new": "x"},
        ]
    }
    applied = apply_code_ops(root, payload)
    assert "app/src/main/java/Demo.kt" in applied
    assert "docs/NOTE.md" in applied
    assert "fun a() = 2" in target.read_text(encoding="utf-8")
    assert (root / "docs/NOTE.md").read_text(encoding="utf-8") == "# hi\n"
    assert payload.get("_skipped_ops")
