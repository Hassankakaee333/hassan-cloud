"""Production entrypoint that composes Hassan Cloud core with Frishta extension routers."""

from __future__ import annotations

from .agent_benchmark_api import build_agent_benchmark_router
from .agent_shadow_api import build_agent_shadow_router
from .agent_verification_api import build_agent_verification_router
from .main import app, files, new_id, now_ms, repo, verify_token

app.include_router(
    build_agent_verification_router(
        repo=repo,
        files=files,
        verify_token=verify_token,
        new_id=new_id,
        now_ms=now_ms,
    )
)
app.include_router(
    build_agent_benchmark_router(
        repo=repo,
        files=files,
        verify_token=verify_token,
        new_id=new_id,
        now_ms=now_ms,
    )
)
app.include_router(
    build_agent_shadow_router(
        repo=repo,
        files=files,
        verify_token=verify_token,
        new_id=new_id,
        now_ms=now_ms,
    )
)
