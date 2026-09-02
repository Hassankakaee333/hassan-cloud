"""Binary artifact storage — local filesystem or PostgreSQL blobs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import FILE_DIR, ensure_dirs

if TYPE_CHECKING:
    pass


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


class DbBlobStore:
    """Store artifact bytes in PostgreSQL — EPHEMERAL on some hosts, durable with managed Postgres."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def save(self, artifact_id: str, filename: str, data: bytes) -> tuple[str, str, int]:
        digest = hashlib.sha256(data).hexdigest()
        self.repo.save_artifact_blob(artifact_id, data)
        return f"db://{artifact_id}", digest, len(data)

    def read(self, storage_path: str) -> bytes:
        if not storage_path.startswith("db://"):
            raise FileNotFoundError(storage_path)
        aid = storage_path.removeprefix("db://")
        data = self.repo.read_artifact_blob(aid)
        if data is None:
            raise FileNotFoundError(aid)
        return data
