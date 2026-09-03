"""Build Frishta AI candidate APK for self-improve cloud jobs.

Clones/finds Android sources, asks Gemini to apply the user's goal as real
source edits, bumps version, builds assembleCandidateDebug, stages APK.
Optionally commits+pushes applied changes back to HASSAN_CANDIDATE_REPO.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Files the coder is allowed to touch (relative to Android root).
EDITABLE_GLOBS = [
    "app/src/main/java/ai/hassan/app/ui/**/*.kt",
    "app/src/main/java/ai/hassan/app/ui/theme/**/*.kt",
    "app/src/main/res/values/*.xml",
    "app/src/main/res/values-night/*.xml",
    "docs/*.md",
]

# Prefer these paths first in the prompt context.
PRIORITY_FILES = [
    "app/src/main/java/ai/hassan/app/ui/theme/HassanTheme.kt",
    "app/src/main/java/ai/hassan/app/ui/HassanApp.kt",
    "app/src/main/java/ai/hassan/app/MainActivity.kt",
    "app/build.gradle.kts",
]


def _find_candidate_root() -> Path | None:
    env_root = os.environ.get("CANDIDATE_APP_ROOT", "").strip()
    if env_root:
        root = Path(env_root)
        if (root / "app" / "build.gradle.kts").exists():
            return root

    here = Path(__file__).resolve()
    for parent in [here.parents[2], here.parents[1], Path.cwd(), Path.cwd().parent]:
        gradle = parent / "app" / "build.gradle.kts"
        if gradle.exists() and "ai.hassan.app" in gradle.read_text(encoding="utf-8", errors="ignore"):
            return parent

    repo = os.environ.get("HASSAN_CANDIDATE_REPO", "").strip()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("HASSAN_CANDIDATE_TOKEN", "")
    if repo:
        dest = Path("/tmp/frishta-candidate-src")
        if dest.exists():
            shutil.rmtree(dest)
        url = repo if repo.startswith("http") else f"https://github.com/{repo}.git"
        if token and url.startswith("https://") and "@" not in url:
            url = url.replace("https://", f"https://x-access-token:{token}@")
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode == 0 and (dest / "app" / "build.gradle.kts").exists():
            return dest
    return None


def _bump_version(gradle_file: Path) -> tuple[int, str]:
    text = gradle_file.read_text(encoding="utf-8")
    code_match = re.search(r"versionCode\s*=\s*(\d+)", text)
    name_match = re.search(r'versionName\s*=\s*"([^"]+)"', text)
    if not code_match:
        raise RuntimeError("versionCode not found in app/build.gradle.kts")
    old_code = int(code_match.group(1))
    new_code = old_code + 1
    old_name = name_match.group(1) if name_match else "0.0.0"
    if "+self" in old_name:
        new_name = re.sub(r"\+self\d*", f"+self{new_code}", old_name)
    else:
        parts = old_name.split(".")
        if parts and parts[-1].isdigit():
            parts[-1] = str(int(parts[-1]) + 1)
            new_name = ".".join(parts)
        else:
            new_name = f"{old_name}+self{new_code}"
    text2 = re.sub(r"versionCode\s*=\s*\d+", f"versionCode = {new_code}", text, count=1)
    text2 = re.sub(r'versionName\s*=\s*"[^"]+"', f'versionName = "{new_name}"', text2, count=1)
    gradle_file.write_text(text2, encoding="utf-8")
    return new_code, new_name


def _append_improve_log(root: Path, job_id: str, goal: str, applied: list[str]) -> str:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / "SELF_IMPROVE_LOG.md"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    files = ", ".join(applied) if applied else "(none — build only)"
    entry = (
        f"\n## {stamp} — job `{job_id}`\n\n"
        f"- Goal: {goal.strip()[:2000]}\n"
        f"- Applied files: {files}\n"
    )
    prev = path.read_text(encoding="utf-8") if path.exists() else "# Frishta Self-Improve Log\n"
    path.write_text(prev + entry, encoding="utf-8")
    return path.as_posix()


def _safe_rel(path: str) -> str | None:
    p = path.replace("\\", "/").lstrip("./")
    if ".." in p.split("/") or p.startswith("/") or ":" in p[:3]:
        return None
    if not (p.startswith("app/") or p.startswith("docs/")):
        return None
    if not (p.endswith(".kt") or p.endswith(".xml") or p.endswith(".md") or p.endswith(".kts")):
        return None
    return p


def _collect_context_files(root: Path, max_chars: int = 90000) -> dict[str, str]:
    files: dict[str, str] = {}
    for rel in PRIORITY_FILES:
        path = root / rel
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            files[rel] = text[:45000] if rel.endswith("HassanApp.kt") else text[:20000]

    # Extra UI files (small)
    ui_root = root / "app/src/main/java/ai/hassan/app/ui"
    if ui_root.is_dir():
        for path in sorted(ui_root.rglob("*.kt")):
            rel = path.relative_to(root).as_posix()
            if rel in files:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if len(text) > 12000:
                continue
            files[rel] = text

    total = 0
    trimmed: dict[str, str] = {}
    for rel, text in files.items():
        if total + len(text) > max_chars:
            break
        trimmed[rel] = text
        total += len(text)
    return trimmed


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Gemini response has no JSON object")
    return json.loads(text[start : end + 1])


def _coder_prompt(goal: str, context_files: dict[str, str]) -> str:
    file_blocks = "\n\n".join(
        f"### FILE: {rel}\n```\n{content}\n```" for rel, content in context_files.items()
    )
    return f"""You are Frishta's on-device self-improve coder for an Android Kotlin + Jetpack Compose app.

USER GOAL (Arabic or English):
{goal.strip()[:3000]}

Apply the goal by editing the candidate app sources. Return ONLY one JSON object (no markdown prose):
{{
  "summary": "short what you changed",
  "files": [
    {{"path": "app/src/main/java/.../File.kt", "action": "write", "content": "full new file content"}},
    {{"path": "app/src/main/java/.../File.kt", "action": "replace", "old": "exact old snippet", "new": "replacement"}}
  ]
}}

Rules:
- Prefer action=replace for small surgical edits; use write only for new files or full rewrites of smaller files.
- Allowed paths only under app/ or docs/, extensions .kt .xml .md .kts
- Do NOT invent fake APK names or break package ai.hassan.app
- Keep Arabic UI strings RTL-friendly
- For frosted-glass / modern dark UI: edit HassanTheme.kt and top/composer bars in HassanApp.kt
- Max 6 file operations
- old must match existing file text exactly when action=replace
- Always add proper Kotlin imports at the top of the file. Never use invalid FQNs like androidx.compose.ui.Modifier.fillMaxSize — use Modifier.fillMaxSize() with import androidx.compose.foundation.layout.fillMaxSize and import androidx.compose.ui.Modifier
- After edits the project MUST compile with :app:assembleCandidateDebug

CURRENT FILES:
{file_blocks}
"""


def _sanitize_applied_kotlin(root: Path) -> list[str]:
    """Repair common Gemini Compose mistakes so the APK can compile."""
    touched: list[str] = []
    java_root = root / "app/src/main/java"
    if not java_root.is_dir():
        return touched
    for path in java_root.rglob("*.kt"):
        src = path.read_text(encoding="utf-8")
        original = src
        src = src.replace(
            "androidx.compose.ui.modifier.fillMaxSize()",
            "Modifier.fillMaxSize()",
        )
        # This app compiles with package import `import androidx.compose.ui.modifier`.
        # Converting it to `.Modifier` breaks the build — always normalize back.
        src = re.sub(
            r"^import androidx\.compose\.ui\.modifier\.Modifier\s*$",
            "import androidx.compose.ui.modifier",
            src,
            flags=re.M,
        )
        has_modifier_import = (
            re.search(r"^import androidx\.compose\.ui\.modifier\s*$", src, re.M) is not None
        )
        needs: list[str] = []
        if (
            re.search(r"\bModifier\.fillMaxSize\s*\(", src)
            and "import androidx.compose.foundation.layout.fillMaxSize" not in src
        ):
            needs.append("import androidx.compose.foundation.layout.fillMaxSize")
        if "MaterialTheme." in src and "import androidx.compose.material3.MaterialTheme" not in src:
            if "androidx.compose.material3.MaterialTheme" not in src:
                needs.append("import androidx.compose.material3.MaterialTheme")
        if re.search(r"(?<!\.)\bSurface\s*\(", src) and "import androidx.compose.material3.Surface" not in src:
            if "androidx.compose.material3.Surface" not in src:
                needs.append("import androidx.compose.material3.Surface")
        if re.search(r"\bModifier\.", src) and not has_modifier_import:
            needs.append("import androidx.compose.ui.Modifier")
        for imp in needs:
            if imp not in src:
                lines = src.splitlines(keepends=True)
                insert_at = 0
                for i, line in enumerate(lines):
                    if line.startswith("package ") or line.startswith("import "):
                        insert_at = i + 1
                lines.insert(insert_at, imp + "\n")
                src = "".join(lines)
        if src != original:
            path.write_text(src, encoding="utf-8")
            touched.append(path.relative_to(root).as_posix())
    return touched


def _build_fix_prompt(goal: str, build_log: str, context_files: dict[str, str]) -> str:
    errors = "\n".join(
        line for line in build_log.splitlines() if "e: file://" in line or "Unresolved" in line or "error:" in line
    )[:4000]
    return _coder_prompt(
        goal
        + "\n\nPREVIOUS BUILD FAILED. Fix ONLY the compile errors below. Prefer action=replace.\n"
        + errors,
        context_files,
    )


def _run_assemble(root: Path) -> subprocess.CompletedProcess[str]:
    gradlew = root / "gradlew"
    if os.name == "nt":
        cmd = ["cmd", "/c", "gradlew.bat", ":app:assembleCandidateDebug", "--no-daemon"]
    else:
        if gradlew.exists():
            gradlew.chmod(gradlew.stat().st_mode | 0o111)
            cmd = [str(gradlew), ":app:assembleCandidateDebug", "--no-daemon"]
        else:
            cmd = ["gradle", ":app:assembleCandidateDebug", "--no-daemon"]
    return subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=1200)


def _call_hassan_cloud_coder(prompt: str) -> dict[str, Any]:
    """Call Worker codegen using httpx (same stack as job callbacks).

    urllib from GitHub Actions is often blocked by Cloudflare with error 1010.
    """
    api = (os.environ.get("HASSAN_API_URL") or "").rstrip("/")
    secret = (os.environ.get("HASSAN_CALLBACK_SECRET") or "").strip()
    if not api or not secret:
        raise RuntimeError("HASSAN_API_URL / HASSAN_CALLBACK_SECRET missing for cloud coder")
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("httpx required for cloud codegen") from exc
    resp = httpx.post(
        f"{api}/v1/internal/codegen",
        json={"prompt": prompt},
        headers={
            "X-Hassan-Callback-Secret": secret,
            "User-Agent": "HassanCloud-GHA-Runner/1.0",
            "Accept": "application/json",
        },
        timeout=180.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"cloud codegen HTTP {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    answer = str(data.get("answer") or "").strip()
    if not answer:
        raise RuntimeError(f"cloud codegen empty: {resp.text[:200]}")
    return _extract_json_object(answer)


def _call_gemini_direct(prompt: str) -> dict[str, Any]:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing")
    model = (os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:400]
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
    data = json.loads(raw)
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    answer = "".join(p.get("text", "") for p in parts).strip()
    if not answer:
        raise RuntimeError("Gemini returned empty coder response")
    return _extract_json_object(answer)


def _call_coder(goal: str, context_files: dict[str, str]) -> dict[str, Any]:
    prompt = _coder_prompt(goal, context_files)
    errors: list[str] = []
    # Prefer Hassan Cloud worker (already has GEMINI_API_KEY) — no extra Actions secret.
    try:
        return _call_hassan_cloud_coder(prompt)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cloud:{exc}")
    try:
        return _call_gemini_direct(prompt)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"direct:{exc}")
    raise RuntimeError("coder unavailable — " + " | ".join(errors))


def _call_gemini(goal: str, context_files: dict[str, str]) -> dict[str, Any]:
    """Back-compat name used by run_candidate_self_improve_job."""
    return _call_coder(goal, context_files)


def apply_code_ops(root: Path, payload: dict[str, Any]) -> list[str]:
    """Apply Gemini file ops. Returns list of relative paths touched."""
    applied: list[str] = []
    skipped: list[str] = []
    ops = payload.get("files") or []
    if not isinstance(ops, list):
        raise RuntimeError("Gemini payload.files must be a list")
    for op in ops[:6]:
        if not isinstance(op, dict):
            continue
        rel = _safe_rel(str(op.get("path") or ""))
        if not rel:
            continue
        action = str(op.get("action") or "write").lower()
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if action == "write":
            content = op.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            target.write_text(content, encoding="utf-8")
            applied.append(rel)
        elif action == "replace":
            old = op.get("old")
            new = op.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or not old:
                continue
            if not target.is_file():
                skipped.append(f"{rel}:missing-file")
                continue
            text = target.read_text(encoding="utf-8")
            if old not in text:
                # Soft-skip mismatched snippets so partial patches can still build.
                skipped.append(f"{rel}:old-not-found")
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            applied.append(rel)
    if skipped:
        # Stash on payload for logging by caller
        payload["_skipped_ops"] = skipped
    return applied


def _push_candidate_changes(root: Path, job_id: str, goal: str, applied: list[str]) -> str:
    token = (os.environ.get("HASSAN_CANDIDATE_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    repo = (os.environ.get("HASSAN_CANDIDATE_REPO") or "").strip()
    if not token or not repo or not applied:
        return "skip-push"
    if not (root / ".git").exists():
        return "skip-push-no-git"
    subprocess.run(["git", "config", "user.email", "frishta-bot@users.noreply.github.com"], cwd=root, check=False)
    subprocess.run(["git", "config", "user.name", "Frishta Self-Improve"], cwd=root, check=False)
    subprocess.run(["git", "add", "-A"], cwd=root, check=False)
    msg = f"self-improve({job_id[:8]}): {goal.strip()[:72]}"
    commit = subprocess.run(["git", "commit", "-m", msg], cwd=root, capture_output=True, text=True)
    if commit.returncode != 0:
        return f"no-commit:{commit.stderr[:120]}"
    # Ensure remote uses token for private/cross-repo push
    slug = repo.replace("https://github.com/", "").removesuffix(".git")
    remote = f"https://x-access-token:{token}@github.com/{slug}.git"
    push = subprocess.run(
        ["git", "push", remote, "HEAD:main"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if push.returncode != 0:
        return f"push-failed:{push.stderr[:200]}"
    return "pushed"


def run_candidate_self_improve_job(
    *,
    job_id: str,
    project_id: str,
    github_run_id: str,
    out_dir: Path,
    update_job: Callable[..., None],
    register_agent: Callable[[str, str, str], None],
    stage_artifact: Callable[[str, str, bytes], None],
    fetch_job_context: Callable[[str], dict],
) -> None:
    context = fetch_job_context(job_id)
    goal = str(context.get("goal") or "Frishta candidate self-improve")

    update_job(
        state="RUNNING",
        log_append="[gha] candidate_self_improve starting\n",
        checkpoint_stage="locate_sources",
    )
    root = _find_candidate_root()
    if root is None:
        msg = (
            "Candidate Android sources not found on the runner. "
            "Set CANDIDATE_APP_ROOT or HASSAN_CANDIDATE_REPO, "
            "or run from a monorepo that contains app/build.gradle.kts."
        )
        register_agent("Planner", "FAILED", msg)
        update_job(
            state="FAILED",
            failure_reason="candidate_sources_missing",
            result_summary=msg,
            log_append=f"[gha] {msg}\n",
        )
        raise RuntimeError(msg)

    register_agent("Planner", "COMPLETE", f"root={root}; goal={goal[:180]}")

    # --- REAL coding step (Gemini applies the goal) ---
    update_job(state="CODING", log_append="[gha] collecting sources for Gemini coder\n", checkpoint_stage="coding")
    context_files = _collect_context_files(root)
    applied: list[str] = []
    coder_summary = ""
    try:
        payload = _call_gemini(goal, context_files)
        coder_summary = str(payload.get("summary") or "")
        applied = apply_code_ops(root, payload)
        stage_artifact(
            "candidate-self-improve-patch.json",
            "application/json",
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        skipped = payload.get("_skipped_ops") or []
        if skipped:
            update_job(log_append=f"[gha] skipped ops: {', '.join(skipped)}\n")
        if not applied:
            raise RuntimeError("Gemini returned no applicable file operations")
        sanitized = _sanitize_applied_kotlin(root)
        if sanitized:
            applied = list(dict.fromkeys(applied + sanitized))
            update_job(log_append=f"[gha] sanitized kotlin imports/FQNs: {', '.join(sanitized)}\n")
        register_agent("Coder", "COMPLETE", f"{coder_summary[:160]} files={applied}")
        update_job(
            log_append=f"[gha] coder applied {len(applied)} file(s): {', '.join(applied)}\n",
            checkpoint_stage="code_applied",
        )
    except Exception as exc:  # noqa: BLE001 — surface honest failure to phone
        register_agent("Coder", "FAILED", str(exc)[:300])
        update_job(
            state="FAILED",
            failure_reason="self_improve_code_apply_failed",
            result_summary=f"تعذر تطبيق التحسين على المصدر: {exc}",
            log_append=f"[gha] coder failed: {exc}\n",
        )
        raise

    push_status = _push_candidate_changes(root, job_id, goal, applied)
    update_job(log_append=f"[gha] source push: {push_status}\n")

    log_path = _append_improve_log(root, job_id, goal, applied)
    version_code, version_name = _bump_version(root / "app" / "build.gradle.kts")
    update_job(
        log_append=f"[gha] bumped versionCode={version_code} versionName={version_name}; log={log_path}\n",
        checkpoint_stage="version_bumped",
    )

    update_job(state="RUNNING", log_append="[gha] assembleCandidateDebug starting\n", checkpoint_stage="building")
    _sanitize_applied_kotlin(root)
    proc = _run_assemble(root)
    if proc.returncode != 0:
        update_job(log_append="[gha] first build failed — sanitize + one coder retry\n")
        _sanitize_applied_kotlin(root)
        try:
            fix_payload = _call_coder(
                goal
                + "\n\nFIX COMPILE ERRORS ONLY. Prefer replace. Build log errors:\n"
                + "\n".join(
                    line
                    for line in (proc.stdout + "\n" + proc.stderr).splitlines()
                    if "e: file://" in line or "Unresolved" in line or "error:" in line.lower()
                )[:3500],
                _collect_context_files(root),
            )
            more = apply_code_ops(root, fix_payload)
            applied = list(dict.fromkeys(applied + more))
            _sanitize_applied_kotlin(root)
            update_job(log_append=f"[gha] retry coder fixed files: {', '.join(more) if more else '(none)'}\n")
        except Exception as retry_exc:  # noqa: BLE001
            update_job(log_append=f"[gha] retry coder skipped: {retry_exc}\n")
        proc = _run_assemble(root)

    build_log = (proc.stdout + "\n" + proc.stderr).encode("utf-8")
    stage_artifact("candidate-self-improve-build-log.txt", "text/plain", build_log)

    if proc.returncode != 0:
        register_agent("Builder", "FAILED", f"assembleCandidateDebug exit={proc.returncode}")
        # Include last compile errors in phone-visible summary
        err_lines = [
            line for line in (proc.stdout + "\n" + proc.stderr).splitlines() if "e: file://" in line or "Unresolved" in line
        ][:6]
        update_job(
            state="FAILED",
            failure_reason="assembleCandidateDebug failed",
            result_summary=(
                "Candidate self-improve build failed after code apply — "
                + ("; ".join(err_lines) if err_lines else "see build log")
            )[:500],
            log_append=f"[gha] assembleCandidateDebug exit={proc.returncode}\n",
        )
        raise RuntimeError("assembleCandidateDebug failed")

    apk_candidates = list(
        (root / "app" / "build" / "outputs" / "apk" / "candidate" / "debug").glob("*.apk")
    )
    if not apk_candidates:
        apk_candidates = list((root / "app" / "build" / "outputs" / "apk").rglob("*candidate*.apk"))
    if not apk_candidates:
        update_job(state="FAILED", failure_reason="APK missing after assembleCandidateDebug")
        raise RuntimeError("APK missing after assembleCandidateDebug")

    apk = apk_candidates[0]
    apk_data = apk.read_bytes()
    stage_artifact(
        "frishta-candidate-debug.apk",
        "application/vnd.android.package-archive",
        apk_data,
    )
    report = {
        "job_id": job_id,
        "project_id": project_id,
        "goal": goal,
        "coder_summary": coder_summary,
        "applied_files": applied,
        "push_status": push_status,
        "version_code": version_code,
        "version_name": version_name,
        "apk_name": apk.name,
        "apk_size": len(apk_data),
        "sha256": hashlib.sha256(apk_data).hexdigest(),
        "github_run_id": github_run_id,
        "source_root": str(root),
    }
    stage_artifact(
        "candidate-self-improve-report.json",
        "application/json",
        json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    register_agent("Builder", "COMPLETE", f"APK {apk.name} size={len(apk_data)}")
    update_job(
        state="VERIFYING",
        result_summary=(
            f"Self-improve applied ({len(applied)} files) → APK {version_name}/{version_code}. "
            f"{coder_summary[:120]}"
        ),
        log_append="[gha] candidate APK staged\n",
        checkpoint_stage="android_artifact_upload",
    )
