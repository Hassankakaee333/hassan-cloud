"""Radar 2.0 — candidate discovery (server-side seed).

Discovery metadata never grants a FREE runtime classification. A repository/license can be
open-source while the actual model, hosting, dependencies, compute, auth, or desired usage
path still has cost or eligibility requirements. Those facts must be verified separately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..storage.repository import DatabaseRepository

from .github_source import discover_from_github

USAGE_COST_UNVERIFIED = "UNVERIFIED"

SEED_CANDIDATES = [
    {
        "name": "Ollama",
        "candidate_type": "MODEL",
        "source": "github",
        "url": "https://github.com/ollama/ollama",
        "license": "MIT",
        "cost_type": USAGE_COST_UNVERIFIED,
        "capabilities": ["chat", "local_inference"],
        "status": "NEW",
        "notes": "Open-source runtime candidate; actual usage cost/compute path unverified",
    },
    {
        "name": "OpenDevin",
        "candidate_type": "CODING_AGENT",
        "source": "github",
        "url": "https://github.com/All-Hands-AI/OpenHands",
        "license": "MIT",
        "cost_type": USAGE_COST_UNVERIFIED,
        "capabilities": ["coding", "automation"],
        "status": "NEW",
        "notes": "Open-source coding-agent candidate; actual usage cost/runtime unverified",
    },
    {
        "name": "llama.cpp",
        "candidate_type": "LIBRARY",
        "source": "github",
        "url": "https://github.com/ggml-org/llama.cpp",
        "license": "MIT",
        "cost_type": USAGE_COST_UNVERIFIED,
        "capabilities": ["inference"],
        "status": "NEW",
        "notes": "Open-source inference library; actual compute/model usage cost unverified",
    },
]


def seed_radar(repo: "DatabaseRepository", new_id, now_ms) -> int:
    count = 0
    all_candidates = list(SEED_CANDIDATES)
    discovered = discover_from_github()
    existing_urls = {c["url"] for c in SEED_CANDIDATES}
    for d in discovered:
        if d["url"] not in existing_urls:
            all_candidates.append(d)
            existing_urls.add(d["url"])
    for c in all_candidates:
        repo.upsert_radar_candidate({
            "id": new_id(),
            "name": c["name"],
            "candidate_type": c["candidate_type"],
            "source": c["source"],
            "url": c["url"],
            "license": c.get("license"),
            "cost_type": c.get("cost_type", USAGE_COST_UNVERIFIED),
            "capabilities": c["capabilities"],
            "status": c["status"],
            "discovered_at": now_ms(),
            "last_evaluated_at": None,
            "notes": c.get("notes"),
        })
        count += 1
    return count
