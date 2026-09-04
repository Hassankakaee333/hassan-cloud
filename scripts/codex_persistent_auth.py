"""Encrypted persistence for Codex auth between explicit GitHub Actions jobs.

Only ciphertext is written to the GitHub Actions cache. Plaintext CODEX_HOME
exists only in the ephemeral runner workspace for the duration of an explicit
Codex job.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC = b"FCA1"
_AAD = b"frishta-codex-auth-cache-v1"


def _key(secret: str) -> bytes:
    value = secret.strip()
    if len(value) < 16:
        raise ValueError("persistent Codex auth requires a non-trivial callback secret")
    return hashlib.sha256(b"frishta-codex-cache-v1\0" + value.encode("utf-8")).digest()


def _pack_home(codex_home: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(codex_home.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(codex_home)
            archive.add(path, arcname=str(relative), recursive=False)
    return buffer.getvalue()


def _unpack_home(payload: bytes, codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    root = codex_home.resolve()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            target = (codex_home / member.name).resolve()
            if root != target and root not in target.parents:
                raise ValueError("unsafe path in encrypted Codex session archive")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            target.write_bytes(source.read())


def save_encrypted_codex_home(codex_home: Path, cache_file: Path, secret: str) -> bool:
    payload = _pack_home(codex_home)
    if not payload:
        return False
    nonce = __import__("os").urandom(12)
    encrypted = AESGCM(_key(secret)).encrypt(nonce, payload, _AAD)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temp = cache_file.with_suffix(cache_file.suffix + ".tmp")
    temp.write_bytes(_MAGIC + nonce + encrypted)
    temp.replace(cache_file)
    return True


def restore_encrypted_codex_home(cache_file: Path, codex_home: Path, secret: str) -> bool:
    if not cache_file.exists() or cache_file.stat().st_size <= len(_MAGIC) + 12:
        return False
    data = cache_file.read_bytes()
    if not data.startswith(_MAGIC):
        return False
    nonce = data[len(_MAGIC):len(_MAGIC) + 12]
    ciphertext = data[len(_MAGIC) + 12:]
    try:
        payload = AESGCM(_key(secret)).decrypt(nonce, ciphertext, _AAD)
        _unpack_home(payload, codex_home)
        return True
    except Exception:
        return False
