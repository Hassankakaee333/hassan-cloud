"""Binary artifact storage — metadata stays in SQLite."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import FILE_DIR, ensure_dirs


class FileStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or FILE_DIR
        ensure_dirs()

    def save(self, artifact_id: str, filename: str, data: bytes) -> tuple[str, str, int]:
        safe_name = Path(filename).name
        rel = f"{artifact_id}/{safe_name}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        return str(rel), digest, len(data)

    def read(self, storage_path: str) -> bytes:
        path = self.root / storage_path
        if not path.exists():
            raise FileNotFoundError(storage_path)
        return path.read_bytes()

    def absolute(self, storage_path: str) -> Path:
        return self.root / storage_path
