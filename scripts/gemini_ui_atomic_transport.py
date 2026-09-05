"""Atomic Gemini official-app transport for Frishta P2.15.

This module keeps GEMINI_EXCHANGE private to the transport. Gemini cannot request it through
phone.command; the public tool allowlist in gemini_ui_job remains unchanged.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import gemini_ui_job as worker
from gemini_ui_hardening import guard_no_secrets

INTERNAL_GEMINI_ACTION = "GEMINI_EXCHANGE"
TOOL_MARKER = "FRISHTA_TOOL:"
FINAL_MARKER = "FRISHTA_FINAL:"
_raw_put_phone_file = worker._put_phone_file


def _put_with_retry(path: str, payload: dict[str, Any], message: str) -> None:
    last: Exception | None = None
    for attempt in range(8):
        try:
            _raw_put_phone_file(path, payload, message)
            return
        except RuntimeError as exc:
            last = exc
            if "GitHub HTTP 409" not in str(exc):
                raise
            time.sleep(0.4 + attempt * 0.3)
    assert last is not None
    raise last


def _internal_gemini_exchange(
    text: str,
    *,
    expected_marker: str,
    nonce: str,
    timeout: float = 180.0,
) -> str:
    """Send exactly one package-locked Gemini exchange through the Phone Agent."""
    guard_no_secrets(text, where="atomic Gemini outbound message")
    if expected_marker not in {TOOL_MARKER, FINAL_MARKER}:
        raise ValueError("atomic Gemini marker is not allowed")
    if not nonce.startswith("s-") or len(nonce) < 10:
        raise ValueError("atomic Gemini nonce is invalid")

    command_id = f"gw-{uuid.uuid4().hex[:20]}"
    now_ms = int(time.time() * 1000)
    local_timeout_ms = min(max(int((timeout - 20.0) * 1000), 5_000), 150_000)
    command = {
        "id": command_id,
        "action": INTERNAL_GEMINI_ACTION,
        "text": text,
        "expectedMarker": expected_marker,
        "nonce": nonce,
        "timeoutMs": local_timeout_ms,
        "requiresConfirmation": False,
        "expiresAtEpochMs": now_ms + max(180_000, int(timeout * 1000) + 30_000),
    }
    _put_with_retry(
        f"inbox/{command_id}.json",
        command,
        f"Frishta internal Gemini exchange {command_id}",
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = worker._read_phone_json(f"outbox/{command_id}.json")
        if result is not None:
            if result.get("status") != "COMPLETED":
                raise RuntimeError(
                    f"Atomic Gemini exchange failed: {result.get('message')} "
                    f"active={result.get('activePackage')}"
                )
            answer = str(result.get("uiTree") or "")
            if expected_marker not in answer or nonce not in answer:
                raise RuntimeError("Atomic Gemini exchange returned no fresh protocol marker")
            guard_no_secrets(answer, where="atomic Gemini protocol payload")
            return answer
        time.sleep(1.0)
    raise TimeoutError("Atomic Gemini exchange timed out waiting for Phone Agent outbox")


def _nonce_for(transport: worker.GeminiTransport) -> str:
    nonce = getattr(transport, "_frishta_atomic_nonce", None)
    if not isinstance(nonce, str):
        nonce = "s-" + uuid.uuid4().hex[:12]
        setattr(transport, "_frishta_atomic_nonce", nonce)
    return nonce


def _atomic_send(self: worker.GeminiTransport, text: str) -> None:
    guard_no_secrets(text, where="Gemini outbound message")
    if not text or len(text) > worker.MAX_TEXT:
        raise ValueError("Gemini message exceeds worker limit")

    nonce = _nonce_for(self)
    # P2.15 is deliberately one safe tool turn: initial request -> FRISHTA_TOOL,
    # TOOL_RESULT -> FRISHTA_FINAL. General multi-tool orchestration is a later phase.
    expected = FINAL_MARKER if text.startswith("TOOL_RESULT=") else TOOL_MARKER
    directive = (
        f"\nFRISHTA_NONCE={nonce}\n"
        "Include this exact nonce as a top-level JSON field named nonce in your next "
        "FRISHTA_TOOL or FRISHTA_FINAL object."
    )
    outbound = text + directive
    if len(outbound) > worker.MAX_TEXT:
        raise ValueError("Gemini message exceeds worker limit after nonce binding")
    answer = _internal_gemini_exchange(
        outbound,
        expected_marker=expected,
        nonce=nonce,
        timeout=180.0,
    )
    setattr(self, "_frishta_atomic_pending", answer)


def _atomic_await_protocol(
    self: worker.GeminiTransport,
    *,
    timeout: float = 120.0,
) -> tuple[str, str]:
    del timeout  # The local atomic exchange already waited for a fresh bound response.
    answer = getattr(self, "_frishta_atomic_pending", None)
    if not isinstance(answer, str) or not answer:
        raise RuntimeError("Atomic Gemini transport has no pending response")
    setattr(self, "_frishta_atomic_pending", None)
    marker = worker._extract_protocol(answer)
    if not marker:
        raise RuntimeError("Atomic Gemini response contained no Frishta protocol marker")
    kind, payload = marker
    nonce = _nonce_for(self)
    if nonce not in payload:
        raise RuntimeError("Atomic Gemini response nonce mismatch")
    guard_no_secrets(payload, where="Gemini protocol payload")
    return kind, payload


def install_atomic_transport() -> None:
    """Install package-locked Gemini transport and conflict-safe phone command writes."""
    if getattr(worker, "_FRISHTA_ATOMIC_TRANSPORT_INSTALLED", False):
        return
    worker._put_phone_file = _put_with_retry
    worker.GeminiTransport.send = _atomic_send
    worker.GeminiTransport.await_protocol = _atomic_await_protocol
    worker._FRISHTA_ATOMIC_TRANSPORT_INSTALLED = True
