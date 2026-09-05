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
    """Retry only transient phone-control branch-head conflicts."""
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


def _editable_target(tree: str) -> str | None:
    """Return the exact non-password editable node id from the verified Gemini tree."""
    for line in tree.splitlines():
        low = line.lower()
        if "editable=true" not in low or "password=true" in low:
            continue
        match = re.search(r"\|id=([^|]+)\|", line)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def _live_send_center(tree: str) -> tuple[int, int] | None:
    """Prefer semantic send labels; fall back only to a tiny adjacent composer control."""
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


def _live_send(self: GeminiTransport, text: str) -> None:
    """Set text directly on Gemini's verified editable node; avoid a stale coordinate tap.

    The old transport read Gemini's tree, waited through the GitHub bridge, then tapped old
    coordinates. A real run proved that the phone could move to Recents before the TAP executed.
    ACTION_SET_TEXT can target Gemini's exact editable node directly, eliminating that race and one
    full phone-control round trip. If foreground drift still happens, re-open Gemini once and retry.
    """
    if not text or len(text) > worker_module.MAX_TEXT:
        raise ValueError("Gemini message exceeds worker limit")
    last_message = ""
    for _ in range(2):
        tree = self._tree(reopen=True)
        target = _editable_target(tree)
        if not target:
            raise RuntimeError("Gemini safe editable field not found")
        result = self.phone.command("SET_TEXT", {"targetText": target, "text": text})
        if result.get("status") == "COMPLETED" and result.get("activePackage") in worker_module.GEMINI_FOREGROUND_PACKAGES:
            tree = self._tree()
            send = _live_send_center(tree)
            if send:
                tapped = self.phone.command("TAP", {"x": send[0], "y": send[1]})
                if tapped.get("status") == "COMPLETED" and tapped.get("activePackage") in worker_module.GEMINI_FOREGROUND_PACKAGES:
                    return
                last_message = f"Gemini send tap lost foreground: {tapped.get('activePackage')}"
                continue
            for label in ("إرسال", "Send"):
                try:
                    clicked = self.phone.command("CLICK_TEXT", {"targetText": label}, timeout=15.0)
                    if clicked.get("status") == "COMPLETED" and clicked.get("activePackage") in worker_module.GEMINI_FOREGROUND_PACKAGES:
                        return
                except Exception:
                    pass
            raise RuntimeError("Gemini send control not found")
        last_message = f"Gemini SET_TEXT failed: {result.get('message')} active={result.get('activePackage')}"
    raise RuntimeError(last_message or "Gemini text entry failed")


def main() -> None:
    worker_module._put_phone_file = _live_put_phone_file
    worker_module._send_center = _live_send_center
    PhoneBridge.command = _live_phone_command
    GeminiTransport.send = _live_send
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
