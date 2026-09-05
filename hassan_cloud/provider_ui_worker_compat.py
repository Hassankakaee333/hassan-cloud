"""Gemini Android package compatibility for the official-app UI worker.

The launcher package can be com.google.android.apps.bard while the foreground Accessibility
package on current Android builds is com.google.android.googlequicksearchbox. Keep launch
behavior in the base worker, but accept either package when validating that Gemini is foreground.
"""
from __future__ import annotations

from .provider_ui_worker import GeminiUiWorker as _BaseGeminiUiWorker

_GEMINI_FOREGROUND_PACKAGES = {
    "com.google.android.apps.bard",
    "com.google.android.googlequicksearchbox",
}


class GeminiUiWorker(_BaseGeminiUiWorker):
    def _tree(self) -> str:
        data = self._phone("UI_TREE")
        active = str(data.get("activePackage") or "")
        if active not in _GEMINI_FOREGROUND_PACKAGES:
            raise RuntimeError(f"Gemini is not foreground; active={active}")
        return str(data.get("uiTree") or "")
