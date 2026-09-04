"""Frishta Agent artifact verifier.

This runner NEVER executes downloaded Agent code. It only resolves/downloads a candidate artifact,
checks integrity and archive paths, and emits structured evidence for Frishta's Evaluation Lab.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

MAX_REDIRECTS = 4
DEFAULT_MAX_BYTES = 120 * 1024 * 1024
HARD_MAX_BYTES = 250 * 1024 * 1024
USER_AGENT = "Frishta-Agent-Artifact-Verifier/1"


def _env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


def _max_bytes() -> int:
    raw = os.environ.get("FRISHTA_MAX_BYTES", "").strip()
    value = int(raw) if raw else DEFAULT_MAX_BYTES
    if value <= 0 or value > HARD_MAX_BYTES:
        raise ValueError("FRISHTA_MAX_BYTES outside safe range")
    return value


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_https_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("artifact URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("artifact URL must not contain credentials")
    if parsed.port not in (None, 443):
        raise ValueError("artifact URL must use default HTTPS port")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        raise ValueError("artifact URL host is not allowed")

    # IP literals must be public. Hostnames are also resolved before every request/redirect to
    # prevent obvious SSRF to loopback/private/link-local destinations.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not infos:
            raise ValueError("artifact host did not resolve")
        ips = {entry[4][0] for entry in infos}
        if not ips or any(not _is_public_ip(ip) for ip in ips):
            raise ValueError("artifact host resolves to a non-public address")
    else:
        if not _is_public_ip(host):
            raise ValueError("artifact URL IP is not public")
    return url


def _request_json(url: str, client: httpx.Client) -> dict:
    validate_public_https_url(url)
    response = client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    response.raise_for_status()
    if len(response.content) > 4 * 1024 * 1024:
        raise ValueError("metadata response too large")
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("metadata response must be an object")
    return data


def download_public_https(url: str, destination: Path, max_bytes: int, client: httpx.Client) -> str:
    current = validate_public_https_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        with client.stream(
            "GET",
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream,*/*"},
            follow_redirects=False,
        ) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect missing Location")
                current = validate_public_https_url(urljoin(current, location))
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > max_bytes:
                raise ValueError("artifact exceeds size limit")
            digest = hashlib.sha256()
            written = 0
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError("artifact exceeds size limit")
                    digest.update(chunk)
                    handle.write(chunk)
            return digest.hexdigest()
    raise ValueError("too many redirects")


def _normalized_member_path(name: str) -> tuple[str, ...]:
    clean = name.replace("\\", "/")
    if clean.startswith("/"):
        raise ValueError("archive contains absolute path")
    parts = tuple(part for part in clean.split("/") if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise ValueError("archive contains path traversal")
    return parts


def inspect_archive(path: Path) -> dict:
    if zipfile.is_zipfile(path):
        count = 0
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                _normalized_member_path(item.filename)
                mode = (item.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError("zip archive contains symlink")
                count += 1
        return {"format": "zip", "members": count, "safe": True}

    if tarfile.is_tarfile(path):
        count = 0
        with tarfile.open(path, "r:*") as archive:
            for item in archive.getmembers():
                _normalized_member_path(item.name)
                if item.isdev() or item.isfifo():
                    raise ValueError("tar archive contains device/fifo entry")
                if item.issym() or item.islnk():
                    _normalized_member_path(item.linkname)
                count += 1
        return {"format": "tar", "members": count, "safe": True}

    # Direct executable/binary release: integrity can still be verified by registry SHA-256.
    return {"format": "direct", "members": 1, "safe": True}


def _npm_name_and_version(package_spec: str) -> tuple[str, str]:
    if package_spec.startswith("@"):
        separator = package_spec.rfind("@")
        if separator <= package_spec.find("/"):
            raise ValueError("scoped npm package must include exact version")
    else:
        separator = package_spec.rfind("@")
        if separator <= 0:
            raise ValueError("npm package must include exact version")
    name = package_spec[:separator]
    version = package_spec[separator + 1 :]
    if not name or not version or version.lower() == "latest":
        raise ValueError("npm package version must be exact")
    return name, version


def _verify_npm_integrity(data: bytes, dist: dict) -> bool:
    integrity = str(dist.get("integrity") or "")
    if integrity.startswith("sha512-"):
        expected = base64.b64decode(integrity[len("sha512-") :])
        return hashlib.sha512(data).digest() == expected
    shasum = str(dist.get("shasum") or "").lower()
    if len(shasum) == 40:
        return hashlib.sha1(data).hexdigest() == shasum
    return False


def _base_report(agent_id: str, static_evidence_id: str, kind: str, version: str) -> dict:
    return {
        "schema_version": 1,
        "agent_id": agent_id,
        "evidence_id": os.environ.get("GITHUB_RUN_ID", "local") or "local",
        "static_evidence_id": static_evidence_id,
        "distribution_kind": kind,
        "version": version,
        "artifact_sha256": "",
        "artifact_executed": False,
        "secrets_used": False,
        "integrity_verified": False,
        "archive_safe": False,
        "dependency_lock_complete": False,
        "passed": False,
        "blockers": [],
        "warnings": [],
    }


def verify_binary(report: dict, client: httpx.Client, temp_dir: Path, max_bytes: int) -> None:
    source_url = _env("FRISHTA_SOURCE_URL")
    expected_sha = _env("FRISHTA_EXPECTED_SHA256").lower()
    if len(expected_sha) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha):
        raise ValueError("binary verification requires valid expected SHA-256")
    target = temp_dir / "artifact.bin"
    actual_sha = download_public_https(source_url, target, max_bytes, client)
    report["source_url"] = source_url
    report["artifact_sha256"] = actual_sha
    if actual_sha != expected_sha:
        report["blockers"].append("sha256_mismatch")
        return
    report["integrity_verified"] = True
    archive = inspect_archive(target)
    report["archive"] = archive
    report["archive_safe"] = bool(archive["safe"])
    report["dependency_lock_complete"] = True
    report["passed"] = report["integrity_verified"] and report["archive_safe"]


def verify_npx(report: dict, client: httpx.Client, temp_dir: Path, max_bytes: int) -> None:
    package_spec = _env("FRISHTA_PACKAGE")
    name, package_version = _npm_name_and_version(package_spec)
    expected_version = _env("FRISHTA_VERSION")
    if package_version != expected_version:
        raise ValueError("npm package version does not match registry agent version")
    metadata_url = f"https://registry.npmjs.org/{quote(name, safe='')}/{quote(package_version, safe='')}"
    metadata = _request_json(metadata_url, client)
    dist = metadata.get("dist") or {}
    tarball = str(dist.get("tarball") or "")
    if not tarball:
        raise ValueError("npm metadata missing tarball")
    target = temp_dir / "package.tgz"
    actual_sha = download_public_https(tarball, target, max_bytes, client)
    data = target.read_bytes()
    report["source_url"] = tarball
    report["artifact_sha256"] = actual_sha
    report["integrity_verified"] = _verify_npm_integrity(data, dist)
    if not report["integrity_verified"]:
        report["blockers"].append("npm_registry_integrity_mismatch")
        return
    archive = inspect_archive(target)
    report["archive"] = archive
    report["archive_safe"] = bool(archive["safe"])
    report["warnings"].append("dependency_lock_pending")
    # Deliberately false: exact top-level tarball verification is not a dependency-tree lock.
    report["dependency_lock_complete"] = False
    report["passed"] = False


def verify_uvx(report: dict, client: httpx.Client, temp_dir: Path, max_bytes: int) -> None:
    package_name = _env("FRISHTA_PACKAGE")
    version = _env("FRISHTA_VERSION")
    if not package_name or any(ch in package_name for ch in " /\\@"):
        raise ValueError("invalid PyPI package name")
    metadata_url = f"https://pypi.org/pypi/{quote(package_name, safe='')}/{quote(version, safe='')}/json"
    metadata = _request_json(metadata_url, client)
    urls = metadata.get("urls") or []
    candidates = [item for item in urls if isinstance(item, dict) and item.get("url")]
    if not candidates:
        raise ValueError("PyPI metadata has no release files")
    # Prefer wheel, then smallest known file. This is artifact-integrity evidence only, not a full
    # uv dependency lock, so the report remains non-approving.
    candidates.sort(key=lambda item: (0 if item.get("packagetype") == "bdist_wheel" else 1, int(item.get("size") or 1 << 60)))
    selected = candidates[0]
    source_url = str(selected["url"])
    target = temp_dir / "package.bin"
    actual_sha = download_public_https(source_url, target, max_bytes, client)
    expected_sha = str((selected.get("digests") or {}).get("sha256") or "").lower()
    report["source_url"] = source_url
    report["artifact_sha256"] = actual_sha
    report["integrity_verified"] = len(expected_sha) == 64 and actual_sha == expected_sha
    if not report["integrity_verified"]:
        report["blockers"].append("pypi_sha256_mismatch")
        return
    archive = inspect_archive(target)
    report["archive"] = archive
    report["archive_safe"] = bool(archive["safe"])
    report["warnings"].append("dependency_lock_pending")
    report["dependency_lock_complete"] = False
    report["passed"] = False


def run() -> tuple[dict, int]:
    agent_id = _env("FRISHTA_AGENT_ID")
    static_evidence_id = _env("FRISHTA_STATIC_EVIDENCE_ID")
    kind = _env("FRISHTA_DISTRIBUTION_KIND").lower()
    version = _env("FRISHTA_VERSION")
    if kind not in {"binary", "npx", "uvx"}:
        raise ValueError("unsupported distribution kind")

    report = _base_report(agent_id, static_evidence_id, kind, version)
    max_bytes = _max_bytes()
    with tempfile.TemporaryDirectory(prefix="frishta-agent-verify-") as tmp:
        temp_dir = Path(tmp)
        with httpx.Client(timeout=httpx.Timeout(60.0, read=180.0), trust_env=False) as client:
            if kind == "binary":
                verify_binary(report, client, temp_dir, max_bytes)
            elif kind == "npx":
                verify_npx(report, client, temp_dir, max_bytes)
            else:
                verify_uvx(report, client, temp_dir, max_bytes)

    # A completed package integrity check can be successful as a verifier operation while still not
    # authorizing SECURITY_CHECKED because dependency_lock_complete=false / passed=false.
    exit_code = 2 if report["blockers"] else 0
    return report, exit_code


def main() -> int:
    out_dir = Path(os.environ.get("FRISHTA_VERIFY_OUT_DIR", "/tmp/frishta-agent-verify"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "agent-artifact-verification.json"
    try:
        report, exit_code = run()
    except Exception as exc:  # evidence must survive even when validation blocks the candidate
        report = {
            "schema_version": 1,
            "agent_id": os.environ.get("FRISHTA_AGENT_ID", ""),
            "evidence_id": os.environ.get("GITHUB_RUN_ID", "local") or "local",
            "static_evidence_id": os.environ.get("FRISHTA_STATIC_EVIDENCE_ID", ""),
            "artifact_executed": False,
            "secrets_used": False,
            "integrity_verified": False,
            "archive_safe": False,
            "dependency_lock_complete": False,
            "passed": False,
            "blockers": [f"verifier_error:{type(exc).__name__}:{exc}"],
            "warnings": [],
        }
        exit_code = 2
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
