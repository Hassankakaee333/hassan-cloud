"""Isolated coding workspace MVP — subprocess prototype, not production sandbox."""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

from ..config import WORKSPACE_DIR, ensure_dirs

FIXTURE_SOURCE = '''def greet(name: str) -> str:
    return f"hello {name}"
'''

FIXTURE_TEST = '''from app import greet

def test_greet():
    assert greet("hassan") == "hello hassan"
'''


def run_coding_workspace(project_id: str, job_id: str) -> dict:
    """Create isolated dir, copy fixture, modify, pytest, zip workspace."""
    ensure_dirs()
    root = (WORKSPACE_DIR / project_id / job_id).resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    src = root / "app.py"
    test = root / "test_app.py"
    src.write_text(FIXTURE_SOURCE, encoding="utf-8")
    test.write_text(FIXTURE_TEST, encoding="utf-8")

    before = src.read_text(encoding="utf-8")
    src.write_text("# Hassan Cloud workspace edit\n" + before, encoding="utf-8")
    after = src.read_text(encoding="utf-8")
    diff = f"--- before\n+++ after\n+ # Hassan Cloud workspace edit\n"

    proc = subprocess.run(
        ["python", "-m", "pytest", "-q", "test_app.py"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=60,
    )

    zip_bytes = _zip_directory(root)
    return {
        "workspace": str(root),
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "diff": diff,
        "zip_bytes": zip_bytes,
        "tests_passed": proc.returncode == 0 and "passed" in (proc.stdout or "").lower(),
    }


def _zip_directory(root: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if path.is_file() and ".pytest_cache" not in path.parts:
                zf.write(path, arcname=str(path.relative_to(root)))
    return buffer.getvalue()
