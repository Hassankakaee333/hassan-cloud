"""Safe end-to-end smoke test for Gemini -> Tool Gateway -> Phone Agent -> Gemini."""
from __future__ import annotations

import json

from gemini_ui_job import GeminiTransport, PhoneBridge, execute_tool


def main() -> None:
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
    transport.send("TOOL_RESULT=" + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    kind, payload = transport.await_protocol(timeout=120.0)
    if kind != "final":
        raise RuntimeError(f"expected FRISHTA_FINAL, got {kind}: {payload[:300]}")
    final = json.loads(payload)
    if final.get("summary") != "gemini-tool-gateway-ok":
        raise RuntimeError(f"unexpected final summary: {final}")
    print("GEMINI_TOOL_GATEWAY_SMOKE_OK")


if __name__ == "__main__":
    main()
