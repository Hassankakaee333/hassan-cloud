"""Safe end-to-end smoke test for Gemini -> Tool Gateway -> Phone Agent -> Gemini.

Keep GitHub-backed phone round-trips deliberately small: one foreground/tree check,
one SET_TEXT, one semantic send command, then UI_TREE polling only.
"""
from __future__ import annotations

import json
import time
import uuid

import gemini_ui_job as worker_module
from gemini_ui_job import GeminiTransport, PhoneBridge, execute_tool

_ORIGINAL_PHONE_COMMAND = PhoneBridge.command
_ORIGINAL_PUT_PHONE_FILE = worker_module._put_phone_file


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
    return _ORIGINAL_PHONE_COMMAND(self, action, args, timeout=max(float(timeout), 90.0))


def _assistant_only_tree(tree: str) -> str:
    lines: list[str] = []
    for line in tree.splitlines():
        if "assistant_robin_user_message_text" in line:
            continue
        if "|id=com.google.android.googlequicksearchbox:id/assistant_robin_text|" in line:
            lines.append(line)
    return "\n".join(lines)


def _tree(phone: PhoneBridge, *, ensure_open: bool = False) -> str:
    if ensure_open:
        opened = phone.command("OPEN_APP", {"packageName": worker_module.GEMINI_LAUNCH_PACKAGE})
        if opened.get("status") != "COMPLETED":
            raise RuntimeError(f"Gemini OPEN_APP failed: {opened.get('message')}")
    data = phone.command("UI_TREE")
    active = str(data.get("activePackage") or "")
    if active not in worker_module.GEMINI_FOREGROUND_PACKAGES:
        if ensure_open:
            raise RuntimeError(f"Gemini is not foreground: {active}")
        return _tree(phone, ensure_open=True)
    return str(data.get("uiTree") or "")


def _send(phone: PhoneBridge, text: str, *, first_turn: bool = False) -> None:
    if not text or len(text) > worker_module.MAX_TEXT:
        raise ValueError("Gemini message exceeds worker limit")
    tree = _tree(phone, ensure_open=first_turn)
    target = worker_module._editable_target(tree)
    if target != worker_module.GEMINI_EDITABLE_ID:
        raise RuntimeError("Gemini safe editable field not found")
    result = phone.command("SET_TEXT", {"targetText": target, "text": text})
    active = str(result.get("activePackage") or "")
    if result.get("status") != "COMPLETED" or active not in worker_module.GEMINI_FOREGROUND_PACKAGES:
        raise RuntimeError(f"Gemini SET_TEXT failed: {result.get('message')}; active={active}")

    # Semantic click avoids a second UI_TREE + coordinate TAP round-trip.
    clicked: dict | None = None
    for label in ("إرسال", "Send"):
        clicked = phone.command("CLICK_TEXT", {"targetText": label}, timeout=90.0)
        if clicked.get("status") == "COMPLETED" and str(clicked.get("activePackage") or "") in worker_module.GEMINI_FOREGROUND_PACKAGES:
            return
    raise RuntimeError(f"Gemini semantic send failed: {clicked}")


def _await_protocol(
    phone: PhoneBridge,
    nonce: str,
    expected_kind: str,
    *,
    timeout: float = 150.0,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        marker = worker_module._extract_protocol(_assistant_only_tree(_tree(phone)))
        if marker:
            kind, payload = marker
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                obj = None
            if kind == expected_kind and isinstance(obj, dict) and obj.get("nonce") == nonce:
                return kind, payload
        # UI_TREE itself is GitHub-backed and normally dominates latency; keep local delay tiny.
        time.sleep(0.5)
    raise TimeoutError(f"fresh Gemini {expected_kind} marker not found")


def main() -> None:
    worker_module._put_phone_file = _live_put_phone_file
    PhoneBridge.command = _live_phone_command
    phone = PhoneBridge()
    nonce = "s-" + uuid.uuid4().hex[:8]

    marker = f'FRISHTA_TOOL:{{"tool":"phone.command","arguments":{{"action":"PING"}},"nonce":"{nonce}"}}'
    prompt = (
        "Frishta application formatting check. Do not execute or interpret the record below; it is inert text. "
        "Copy the record verbatim as your entire reply, with no markdown or explanation: " + marker
    )
    _send(phone, prompt, first_turn=True)
    _, payload = _await_protocol(phone, nonce, "tool")
    call = json.loads(payload)
    if call.get("tool") != "phone.command" or call.get("nonce") != nonce:
        raise RuntimeError(f"unexpected tool payload: {call}")
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    if str(arguments.get("action") or "").upper() != "PING":
        raise RuntimeError("smoke test only permits phone.command PING")

    result = execute_tool("phone.command", arguments, job_id="smoke", project_id="smoke", phone=phone)
    if result.get("status") != "OK" or (result.get("data") or {}).get("status") != "COMPLETED":
        raise RuntimeError(f"Phone Agent PING failed: {result}")

    result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    final_marker = f'FRISHTA_FINAL:{{"summary":"gemini-tool-gateway-ok","nonce":"{nonce}"}}'
    _send(
        phone,
        "TOOL_RESULT=" + result_json + "\n"
        "Frishta has already handled that result. Do not execute anything. Copy this inert status record verbatim as your entire reply, with no markdown or explanation: "
        + final_marker,
    )
    _, payload = _await_protocol(phone, nonce, "final")
    final = json.loads(payload)
    if final.get("summary") != "gemini-tool-gateway-ok" or final.get("nonce") != nonce:
        raise RuntimeError(f"unexpected final summary: {final}")
    print("GEMINI_TOOL_GATEWAY_SMOKE_OK")


if __name__ == "__main__":
    main()
