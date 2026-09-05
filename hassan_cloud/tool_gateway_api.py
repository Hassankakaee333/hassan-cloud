"""Authenticated HTTP surface for the provider-neutral Frishta Tool Gateway."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .tool_gateway import ToolGateway, tool_catalog


class ToolInvokeRequest(BaseModel):
    tool: str = Field(..., min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = Field(default=None, max_length=128)


def build_tool_gateway_router(*, repo, verify_token, new_id: Callable[[], str], now_ms: Callable[[], int]) -> APIRouter:
    router = APIRouter(prefix="/v1/tool-gateway", tags=["tool-gateway"])
    gateway = ToolGateway(repo, new_id, now_ms)

    @router.get("/catalog")
    def catalog(_token: str = Depends(verify_token)) -> dict[str, Any]:
        return {
            "version": "1",
            "mode": "provider-neutral",
            "tools": tool_catalog(),
            "rules": {
                "stable_write": False,
                "candidate_write_only": True,
                "phone_secret_input": False,
                "provider_credentials_exposed": False,
            },
        }

    @router.post("/invoke")
    def invoke(body: ToolInvokeRequest, _token: str = Depends(verify_token)) -> dict[str, Any]:
        return gateway.invoke(body.tool, body.arguments, body.call_id)

    return router
