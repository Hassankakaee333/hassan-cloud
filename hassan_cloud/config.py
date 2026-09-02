"""Hassan Cloud configuration — portable paths, no secrets."""

from __future__ import annotations

import os
from pathlib import Path

ENV = os.environ.get("HASSAN_ENV", "development")
DATA_DIR = Path(os.environ.get("HASSAN_DATA_DIR", "data"))
DB_PATH = Path(os.environ.get("HASSAN_DB_PATH", str(DATA_DIR / "hassan_cloud.db")))
FILE_DIR = Path(os.environ.get("HASSAN_FILE_DIR", str(DATA_DIR / "files")))
WORKSPACE_DIR = Path(os.environ.get("HASSAN_WORKSPACE_DIR", str(DATA_DIR / "workspaces")))

# Optional one-time bootstrap (set on host, never commit). Hashed before storage.
BOOTSTRAP_TOKEN = os.environ.get("HASSAN_BOOTSTRAP_TOKEN", "").strip()
DEV_TOKEN = os.environ.get("HASSAN_DEV_TOKEN", "").strip() if ENV == "development" else ""


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
