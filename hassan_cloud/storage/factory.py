"""Repository + artifact store factory — host-independent."""

from __future__ import annotations

import os

from ..config import DB_BACKEND, FILE_DIR, ensure_dirs
from .files import DbBlobStore, FileStore
from .postgres_repository import PostgresRepository
from .repository import DatabaseRepository


def get_repository():
    ensure_dirs()
    if DB_BACKEND == "postgres":
        return PostgresRepository()
    return DatabaseRepository()


def get_file_store(repo):
    mode = os.environ.get("HASSAN_ARTIFACT_STORE", "local").lower()
    if mode == "db" or DB_BACKEND == "postgres":
        return DbBlobStore(repo)
    return FileStore()
