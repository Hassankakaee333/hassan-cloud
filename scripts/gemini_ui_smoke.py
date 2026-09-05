"""One-shot atomic E2E smoke: Gemini -> Tool Gateway -> Phone Agent PING -> Gemini.

Uses the same package-locked production transport as the Gemini worker. No coordinate/UI
micro-command loop is used by this smoke.
"""
from __future__ import annotations

import json

import gemini_ui_job as worker
from gemini_ui_atomic_transport import install_atomic_transport
from gemini_ui_hardening import install_runtime_hardening


def main() -> None:
    install_runtime_hardening()
    install_atomic_transport()

    phone = worker.PhoneBridge()
    gemini = worker.GeminiTransport(phone)

    prompt = (
        "Frishta controlled tool-gateway smoke. Return exactly one machine-readable tool request "
        "and no markdown or explanation: "
        'FRISHTA_TOOL:{"tool":"phone.command","arguments":{"action":"PING"}}'
    )
    gemini.send(prompt)
    kind, payload = gemini.await_protocol()
    if kind != "tool":
        raise RuntimeError(f"expected FRISHTA_TOOL, got {kind}")

    call = json.loads(payload)
    nonce = getattr(gemini, "_frishta_atomic_nonce", "")
    if call.get("tool") != "phone.command" or call.get("nonce") != nonce:
        raise RuntimeError(f"unexpected tool payload: {call}")
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    if str(arguments.get("action") or "").upper() != "PING":
        raise RuntimeError("smoke test only permits phone.command PING")

    result = worker.execute_tool(
        "phone.command",
        arguments,
        job_id="smoke",
        project_id="smoke",
        phone=phone,
    )
    if result.get("status") != "OK" or (result.get("data") or {}).get("status") != "COMPLETED":
        raise RuntimeError(f"Phone Agent PING failed: {result}")

    result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    gemini.send(
        "TOOL_RESULT=" + result_json + "\n"
        "The tool result has already been handled by Frishta. Return exactly this final status, "
        "adding the required bound nonce field and no markdown or explanation: "
        'FRISHTA_FINAL:{"summary":"gemini-tool-gateway-ok"}'
    )
    kind, payload = gemini.await_protocol()
    if kind != "final":
        raise RuntimeError(f"expected FRISHTA_FINAL, got {kind}")
    final = json.loads(payload)
    if final.get("summary") != "gemini-tool-gateway-ok" or final.get("nonce") != nonce:
        raise RuntimeError(f"unexpected final summary: {final}")

    print("GEMINI_ATOMIC_TOOL_GATEWAY_SMOKE_OK")


if __name__ == "__main__":
    main()
