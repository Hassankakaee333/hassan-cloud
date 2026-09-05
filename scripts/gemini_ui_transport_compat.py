from __future__ import annotations

import time

import gemini_ui_job as base


def _looks_submitted(tree: str, text: str) -> bool:
    prefix = " ".join(text.strip().split())[:48]
    normalized_tree = " ".join(tree.split())
    return bool(prefix and "assistant_robin_user_message_text" in tree and prefix in normalized_tree)


class GeminiTransport(base.GeminiTransport):
    def send(self, text: str) -> None:
        if not text or len(text) > base.MAX_TEXT:
            raise ValueError("Gemini message exceeds worker limit")
        tree = self._tree(reopen=True)
        editable = base._editable_center(tree)
        if not editable:
            raise RuntimeError("Gemini safe editable field not found")
        self.phone.command("TAP", {"x": editable[0], "y": editable[1]})
        result = self.phone.command("SET_TEXT", {"targetText": "", "text": text})
        if result.get("status") != "COMPLETED":
            raise RuntimeError(f"Gemini SET_TEXT failed: {result.get('message')}")
        time.sleep(1.0)
        tree = self._tree()
        if _looks_submitted(tree, text):
            return
        send = base._send_center(tree)
        if send:
            self.phone.command("TAP", {"x": send[0], "y": send[1]})
            time.sleep(0.8)
            if _looks_submitted(self._tree(), text):
                return
        for label in ("إرسال", "Send"):
            try:
                clicked = self.phone.command("CLICK_TEXT", {"targetText": label}, timeout=15.0)
                if clicked.get("status") == "COMPLETED":
                    time.sleep(0.8)
                    if _looks_submitted(self._tree(), text):
                        return
            except Exception:
                pass
        raise RuntimeError("Gemini send control not found and submission was not verified")


def run_gemini_ui_job(**kwargs):
    original = base.GeminiTransport
    base.GeminiTransport = GeminiTransport
    try:
        return base.run_gemini_ui_job(**kwargs)
    finally:
        base.GeminiTransport = original
