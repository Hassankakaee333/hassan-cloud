"""Safe end-to-end smoke test for Gemini -> Tool Gateway -> Phone Agent -> Gemini."""
from __future__ import annotations

import json
import re
import time

import gemini_ui_job as worker_module
from gemini_ui_job import GeminiTransport, PhoneBridge, execute_tool

_ORIGINAL_SEND_CENTER = worker_module._send_center
_ORIGINAL_PHONE_COMMAND = PhoneBridge.command
_ORIGINAL_PUT_PHONE_FILE = worker_module._put_phone_file


def _bounds(line: str) -> tuple[int, int, int, int] | None:
    match = re.search(r"bounds=(\d+) (\d+) (\d+) (\d+)", line)
    return tuple(map(int, match.groups())) if match else None


def _live_put_phone_file(path: str, value: dict, message: str) -> None:
    """Retry only transient phone-control branch-head conflicts.

    Phone Agent publishes status/outbox commits on the same branch. GitHub Contents API can reject
    a concurrent create with 409 when that branch head moves between resolution and commit. Retrying
    the same exact new inbox path is safe and does not broaden the worker's authority.
    """
    last: Exception | None = None
    for attempt in range(8):
        try:
            _ORIGINAL_PUT_PHONE_FILE(path, value, message)
            return
        except RuntimeError as exc:
            last = exc
            if "GitHub HTTP 409" not in str(exc):
                raise
            time.sleep(0.5 + attempt * 0.35)
    assert last is not None
    raise last


def _live_phone_command(
    self: PhoneBridge,
    action: str,
    args: dict | None = None,
    *,
    timeout: float = 45.0,
) -> dict:
    """Give the GitHub-backed Phone Agent enough time to publish its exact outbox result."""
    return _ORIGINAL_PHONE_COMMAND(self, action, args, timeout=max(float(timeout), 90.0))


def _live_send_center(tree: str) -> tuple[int, int] | None:
    """Prefer semantic send labels; fall back only to a tiny adjacent composer control.

    Current Gemini Android builds sometimes expose the send icon without an accessibility label.
    The fallback is intentionally constrained to a small control in the same vertical band as the
    non-password editable composer, so the smoke never taps arbitrary page/system controls.
    """
    semantic = _ORIGINAL_SEND_CENTER(tree)
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
            edge_distance = min(abs((x1 + x2) // 2 - ex1), abs((x1 + x2) // 2 - ex2))
            gap = edge_distance
        if gap > 260:
            continue
        cx = (x1 + x2) // 2
        score = gap * 1000 + width * height
        candidates.append((score, cx, cy))

    if not candidates:
        return None
    _, cx, cy = min(candidates)
    return cx, cy


def main() -> None:
    # Patch only this live smoke transport. Production remains fail-closed; after the proof these
    # bounded transport hardenings are promoted into the worker with the same contract tests.
    worker_module._put_phone_file = _live_put_phone_file
    worker_module._send_center = _live_send_center
    PhoneBridge.command = _live_phone_command
    phone = PhoneBridge()
    transport = GeminiTransport(phone)
    prompt = (
        "Frishta transport smoke test. Do not do anything except the protocol. "
        "First reply exactly with this one tool call: "
        'FRISHTA_TOOL:{"tool":"phone.command","arguments":{"action":"PING","args":{}}}. '
        "After you receive TOOL_RESULT, reply exactly: "
        'FRISHTA_FINAL:{"summary":"gemini-tool-gateway-ok"}'
    )
    transport.send(prompt)
    kind, payload = transport.await_protocol(timeout=120.0)
    if kind != "tool":
        raise RuntimeError(f"expected FRISHTA_TOOL, got {kind}: {payload[:300]}")
    call = json.loads(payload)
    if call.get("tool") != "phone.command":
        raise RuntimeError(f"unexpected tool: {call.get('tool')}")
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    if str(arguments.get("action") or "").upper() != "PING":
        raise RuntimeError("smoke test only permits phone.command PING")
    result = execute_tool("phone.command", arguments, job_id="smoke", project_id="smoke", phone=phone)
    if result.get("status") != "OK" or (result.get("data") or {}).get("status") != "COMPLETED":
        raise RuntimeError(f"Phone Agent PING failed: {result}")
    result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    transport.send(
        "TOOL_RESULT=" + result_json + "\n"
        "PROTOCOL REQUIREMENT: Do not explain or paraphrase this result. Your entire next reply must be exactly: "
        'FRISHTA_FINAL:{"summary":"gemini-tool-gateway-ok"}'
    )
    kind, payload = transport.await_protocol(timeout=120.0)
    if kind != "final":
        raise RuntimeError(f"expected FRISHTA_FINAL, got {kind}: {payload[:300]}")
    final = json.loads(payload)
    if final.get("summary") != "gemini-tool-gateway-ok":
        raise RuntimeError(f"unexpected final summary: {final}")
    print("GEMINI_TOOL_GATEWAY_SMOKE_OK")


if __name__ == "__main__":
    main()
