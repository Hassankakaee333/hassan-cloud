"""Shared helpers — no secrets."""

from __future__ import annotations

import time
import uuid


def new_id() -> str:
    return str(uuid.uuid4())


def now_ms() -> int:
    return int(time.time() * 1000)
