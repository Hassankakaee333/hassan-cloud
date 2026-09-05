"""Runtime hardening for the Frishta Gemini official-app worker.

This layer is intentionally fail-closed. It prevents provider text, user goals, tool arguments,
and outbound tool results from carrying credential-like material, and replaces coordinate-first
text entry with a verified editable-node flow that re-checks Gemini foreground ownership.
"""
from __future__ import annotations

import json
import re
from typing import Any

import gemini_ui_job as worker

_SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_flatten_text(k) + "\n" + _flatten_text(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten_text(v) for v in value)
    if value is None or isinstance(value, (bool, int, float)):
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def contains_secret_like(value: Any) -> bool:
    text = _flatten_text(value)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def guard_no_secrets(value: Any, *, where: str) -> None:
    if contains_secret_like(value):
        raise ValueError(f"credential-like material blocked at {where}")


def _editable_target(tree: str) -> str | None:
    for line in tree.splitlines():
        low = line.lower()
        if "editable=true" not in low or "password=true" in low:
            continue
        match = re.search(r"\|id=([^|]+)\|", line)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def _bounds(line: str) -> tuple[int, int, int, int] | None:
    match = re.search(r"bounds=(\d+) (\d+) (\d+) (\d+)", line)
    return tuple(map(int, match.groups())) if match else None


def _safe_send_center(tree: str) -> tuple[int, int] | None:
    semantic = worker._send_center(tree)
    if semantic:
        return semantic

    editable_rect: tuple[int, int, int, int] | None = None
    for line in tree.splitlines():
        low = line.lower()
        if "editable=true" in low and "password=true" not in low:
            editable_rect = _bounds(line)
            if editable_rect:
                break
    if not editable_rect:
        return None

    ex1, ey1, ex2, ey2 = editable_rect
    candidates: list[tuple[int, int, int]] = []
    for line in tree.splitlines():
        low = line.lower()
        rect = _bounds(line)
        if not rect:
            continue
        x1, y1, x2, y2 = rect
        width, height = x2 - x1, y2 - y1
        if width <= 0 or height <= 0 or width > 220 or height > 220:
            continue
        if not ("clickable=true" in low or "button" in low or "imagebutton" in low):
            continue
        cy = (y1 + y2) // 2
        if cy < ey1 - 60 or cy > ey2 + 60:
            continue
        if x2 <= ex1:
            gap = ex1 - x2
        elif x1 >= ex2:
            gap = x1 - ex2
        else:
            gap = min(abs((x1 + x2) // 2 - ex1), abs((x1 + x2) // 2 - ex2))
        if gap > 260:
            continue
        cx = (x1 + x2) // 2
        candidates.append((gap * 1000 + width * height, cx, cy))

    if not candidates:
        return None
    _, cx, cy = min(candidates)
    return cx, cy


def _safe_send(self: worker.GeminiTransport, text: str) -> None:
    guard_no_secrets(text, where="Gemini outbound message")
    if not text or len(text) > worker.MAX_TEXT:
        raise ValueError("Gemini message exceeds worker limit")

    last_message = ""
    for _ in range(2):
        tree = self._tree(reopen=True)
        target = _editable_target(tree)
        if not target:
            raise RuntimeError("Gemini safe editable field not found")
        result = self.phone.command("SET_TEXT", {"targetText": target, "text": text})
        if result.get("status") != "COMPLETED" or result.get("activePackage") not in worker.GEMINI_FOREGROUND_PACKAGES:
            last_message = f"Gemini SET_TEXT failed: {result.get('message')} active={result.get('activePackage')}"
            continue

        tree = self._tree()
        send = _safe_send_center(tree)
        if send:
            tapped = self.phone.command("TAP", {"x": send[0], "y": send[1]})
            if tapped.get("status") == "COMPLETED" and tapped.get("activePackage") in worker.GEMINI_FOREGROUND_PACKAGES:
                return
            last_message = f"Gemini send tap lost foreground: {tapped.get('activePackage')}"
            continue

        for label in ("إرسال", "Send"):
            try:
                clicked = self.phone.command("CLICK_TEXT", {"targetText": label}, timeout=15.0)
                if clicked.get("status") == "COMPLETED" and clicked.get("activePackage") in worker.GEMINI_FOREGROUND_PACKAGES:
                    return
            except Exception:
                pass
        raise RuntimeError("Gemini send control not found")

    raise RuntimeError(last_message or "Gemini text entry failed")


def install_runtime_hardening() -> None:
    if getattr(worker, "_FRISHTA_HARDENING_INSTALLED", False):
        return

    original_initial_prompt = worker.initial_prompt
    original_execute_tool = worker.execute_tool
    original_await_protocol = worker.GeminiTransport.await_protocol

    def safe_initial_prompt(goal: str) -> str:
        guard_no_secrets(goal, where="Gemini job goal")
        prompt = original_initial_prompt(goal)
        guard_no_secrets(prompt, where="Gemini initial prompt")
        return prompt

    def safe_execute_tool(tool: str, arguments: dict[str, Any], *, job_id: str, project_id: str, phone: worker.PhoneBridge) -> dict[str, Any]:
        try:
            guard_no_secrets(arguments, where="Gemini tool arguments")
        except ValueError as exc:
            return {"status": "REJECTED", "tool": tool, "detail": str(exc)}
        return original_execute_tool(tool, arguments, job_id=job_id, project_id=project_id, phone=phone)

    def safe_await_protocol(self: worker.GeminiTransport, *, timeout: float = 120.0) -> tuple[str, str]:
        kind, payload = original_await_protocol(self, timeout=timeout)
        guard_no_secrets(payload, where="Gemini protocol payload")
        return kind, payload

    worker.initial_prompt = safe_initial_prompt
    worker.execute_tool = safe_execute_tool
    worker.GeminiTransport.await_protocol = safe_await_protocol
    worker.GeminiTransport.send = _safe_send
    worker._FRISHTA_HARDENING_INSTALLED = True
