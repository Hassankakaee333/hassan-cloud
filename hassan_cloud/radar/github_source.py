"""GitHub-based RadarSource — discovers real OSS candidates."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("hassan.radar.github")

SEARCH_QUERIES = [
    ("coding agent stars:>500", "CODING_AGENT"),
    ("autonomous agent llm stars:>300", "AUTOMATION_TOOL"),
    ("local llm inference stars:>1000", "MODEL"),
]


def discover_from_github(max_per_query: int = 3) -> list[dict]:
    results: list[dict] = []
    seen_urls: set[str] = set()
    try:
        with httpx.Client(timeout=20.0, headers={"Accept": "application/vnd.github+json"}) as client:
            for query, candidate_type in SEARCH_QUERIES:
                resp = client.get(
                    "https://api.github.com/search/repositories",
                    params={"q": query, "sort": "stars", "order": "desc", "per_page": max_per_query},
                )
                if resp.status_code != 200:
                    logger.warning("GitHub search failed: %s", resp.status_code)
                    continue
                for item in resp.json().get("items", []):
                    url = item.get("html_url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    results.append({
                        "name": item.get("full_name", item.get("name", "unknown")),
                        "candidate_type": candidate_type,
                        "source": "github_search",
                        "url": url,
                        "license": (item.get("license") or {}).get("spdx_id") or "UNKNOWN",
                        "cost_type": "FREE",
                        "capabilities": ["coding"] if "CODING" in candidate_type else ["inference"],
                        "status": "NEW",
                        "notes": f"stars={item.get('stargazers_count', 0)} query={query}",
                        "score": min(10.0, (item.get("stargazers_count", 0) / 1000)),
                        "risk": "MEDIUM",
                    })
    except Exception:
        logger.exception("GitHub radar discovery failed")
    return results
