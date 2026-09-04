from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agent_artifact_verify_runner import (  # noqa: E402
    _npm_name_and_version,
    inspect_archive,
    validate_public_https_url,
)


def test_rejects_private_and_non_https_urls() -> None:
    with pytest.raises(ValueError):
        validate_public_https_url("http://8.8.8.8/file")
    with pytest.raises(ValueError):
        validate_public_https_url("https://127.0.0.1/file")
    with pytest.raises(ValueError):
        validate_public_https_url("https://10.0.0.5/file")
    with pytest.raises(ValueError):
        validate_public_https_url("https://localhost/file")


def test_accepts_public_https_ip_literal() -> None:
    assert validate_public_https_url("https://8.8.8.8/file") == "https://8.8.8.8/file"


def test_npm_version_must_be_exact() -> None:
    assert _npm_name_and_version("agent@1.2.3") == ("agent", "1.2.3")
    assert _npm_name_and_version("@scope/agent@2.0.0") == ("@scope/agent", "2.0.0")
    with pytest.raises(ValueError):
        _npm_name_and_version("agent")
    with pytest.raises(ValueError):
        _npm_name_and_version("agent@latest")


def test_safe_zip_is_inspected_without_extraction(tmp_path: Path) -> None:
    archive_path = tmp_path / "safe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("bin/agent", b"not-executed")
    result = inspect_archive(archive_path)
    assert result == {"format": "zip", "members": 1, "safe": True}


def test_zip_path_traversal_is_blocked(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape", b"x")
    with pytest.raises(ValueError, match="path traversal"):
        inspect_archive(archive_path)


def test_tar_symlink_escape_is_blocked(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.tar"
    with tarfile.open(archive_path, "w") as archive:
        info = tarfile.TarInfo("bin/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        archive.addfile(info, io.BytesIO())
    with pytest.raises(ValueError, match="path traversal"):
        inspect_archive(archive_path)


def test_direct_binary_is_not_executed_or_extracted(tmp_path: Path) -> None:
    binary = tmp_path / "agent.bin"
    binary.write_bytes(b"opaque bytes")
    result = inspect_archive(binary)
    assert result == {"format": "direct", "members": 1, "safe": True}
