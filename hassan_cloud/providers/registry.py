"""Free-first provider registry (server-side)."""

from __future__ import annotations

PROVIDERS = [
    {
        "id": "hassan-honest-chat",
        "capabilities": ["chat"],
        "status": "NOT_CONFIGURED",
        "cost_type": "FREE",
        "health": "HEALTHY",
        "quality_tier": "basic",
        "limits": {"note": "No paid LLM configured — honest fallback only"},
    },
    {
        "id": "local-coding-worker",
        "capabilities": ["coding", "testing"],
        "status": "WORKING",
        "cost_type": "FREE",
        "health": "HEALTHY",
        "quality_tier": "mvp",
        "limits": {"isolation": "subprocess workspace"},
    },
    {
        "id": "github-radar",
        "capabilities": ["research", "discovery"],
        "status": "WORKING",
        "cost_type": "FREE",
        "health": "HEALTHY",
        "quality_tier": "basic",
        "limits": {},
    },
]


def list_providers() -> list[dict]:
    return PROVIDERS


def select_for_capability(capability: str) -> list[dict]:
    return [p for p in PROVIDERS if capability in p.get("capabilities", [])]
