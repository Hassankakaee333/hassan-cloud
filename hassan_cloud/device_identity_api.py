"""Zero-PC enrollment/rotation API for Hassan's Android Keystore identity."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .device_identity import (
    DeviceIdentityRecord,
    DeviceIdentityStore,
    assert_fresh_timestamp,
    canonical_enrollment,
    canonical_rotation,
    public_key_fingerprint,
    verify_signature,
)
from .device_token_binding import DeviceTokenBindingStore


class DeviceEnrollRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    public_key_base64: str = Field(min_length=32, max_length=8192)
    timestamp_ms: int
    signature_base64: str = Field(min_length=8, max_length=1024)


class DeviceRotateRequest(BaseModel):
    new_public_key_base64: str = Field(min_length=32, max_length=8192)
    timestamp_ms: int
    prior_signature_base64: str = Field(min_length=8, max_length=1024)
    new_signature_base64: str = Field(min_length=8, max_length=1024)


def build_device_identity_router(*, repo: Any, verify_token: Callable[..., Any], now_ms: Callable[[], int]) -> APIRouter:
    router = APIRouter()
    store = DeviceIdentityStore(repo)
    token_bindings = DeviceTokenBindingStore(repo)

    @router.get("/v1/device-identities/{device_id}")
    def get_device_identity(device_id: str, _token: str = Depends(verify_token)) -> dict[str, Any]:
        record = store.get(device_id)
        if record is None:
            raise HTTPException(status_code=404, detail="device identity not enrolled")
        return {
            "device_id": record.device_id,
            "fingerprint_sha256": record.fingerprint_sha256,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @router.post("/v1/device-identities/enroll")
    def enroll_device(body: DeviceEnrollRequest, _token: str = Depends(verify_token)) -> dict[str, Any]:
        try:
            assert_fresh_timestamp(body.timestamp_ms, now_ms())
            fingerprint = public_key_fingerprint(body.public_key_base64)
            payload = canonical_enrollment(body.device_id, fingerprint, body.timestamp_ms)
            verify_signature(body.public_key_base64, payload, body.signature_base64)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            token_binding_status = token_bindings.bind_or_verify(_token, body.device_id)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        existing = store.get(body.device_id)
        if existing:
            if existing.fingerprint_sha256 == fingerprint:
                return {
                    "status": "EXISTING",
                    "device_id": body.device_id,
                    "fingerprint_sha256": fingerprint,
                    "bearer_device_binding": token_binding_status,
                }
            raise HTTPException(status_code=409, detail="device identity already pinned; use rotation")
        ts = now_ms()
        created = store.create_once(DeviceIdentityRecord(body.device_id, body.public_key_base64, fingerprint, ts, ts))
        if not created:
            raise HTTPException(status_code=409, detail="device identity enrollment race")
        return {
            "status": "ENROLLED",
            "device_id": body.device_id,
            "fingerprint_sha256": fingerprint,
            "bearer_device_binding": token_binding_status,
        }

    @router.post("/v1/device-identities/{device_id}/rotate")
    def rotate_device(device_id: str, body: DeviceRotateRequest, _token: str = Depends(verify_token)) -> dict[str, Any]:
        current = store.get(device_id)
        if current is None:
            raise HTTPException(status_code=404, detail="device identity not enrolled")
        try:
            token_bindings.require_bound(_token, device_id)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            assert_fresh_timestamp(body.timestamp_ms, now_ms())
            new_fingerprint = public_key_fingerprint(body.new_public_key_base64)
            payload = canonical_rotation(device_id, current.fingerprint_sha256, new_fingerprint, body.timestamp_ms)
            verify_signature(current.public_key_base64, payload, body.prior_signature_base64)
            verify_signature(body.new_public_key_base64, payload, body.new_signature_base64)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if new_fingerprint == current.fingerprint_sha256:
            return {"status": "UNCHANGED", "device_id": device_id, "fingerprint_sha256": new_fingerprint}
        changed = store.rotate(
            device_id,
            current.fingerprint_sha256,
            body.new_public_key_base64,
            new_fingerprint,
            now_ms(),
        )
        if not changed:
            raise HTTPException(status_code=409, detail="device identity changed concurrently")
        return {"status": "ROTATED", "device_id": device_id, "fingerprint_sha256": new_fingerprint}

    return router
