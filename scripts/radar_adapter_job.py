"""Reviewed Radar executable adapters for Hassan Cloud jobs.

This module intentionally does not execute code from discovered repositories. Each
adapter is hard-whitelisted, version-pinned, and runs in a child process with secrets
removed from its environment. The first adapter is a local-page Playwright smoke test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

PLAYWRIGHT_ADAPTER_ID = "playwright-browser-ci-v1"
PLAYWRIGHT_VERSION = "1.63.0"
PLAYWRIGHT_REPOSITORY = "https://github.com/microsoft/playwright"
ALLOWED_ACTION = "smoke"

_BROWSER_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

_SECRET_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "HASSAN_",
    "GEMINI_",
    "OPENAI_",
    "GITHUB_",
    "CODEX_",
)

_SAFE_ENV_KEYS = {
    "PATH",
    "HOME",
    "CI",
    "LANG",
    "LC_ALL",
    "RUNNER_TEMP",
    "RUNNER_TOOL_CACHE",
    "LD_LIBRARY_PATH",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
    "TMP",
    "TEMP",
    "NODE_OPTIONS",
}


def parse_adapter_request(goal: str) -> dict[str, str]:
    """Parse the structured job goal and reject any unreviewed adapter/action."""
    try:
        payload = json.loads(goal)
    except json.JSONDecodeError as exc:
        raise ValueError("radar_adapter goal must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("radar_adapter goal must be a JSON object")

    adapter_id = str(payload.get("adapter_id") or "").strip()
    action = str(payload.get("action") or ALLOWED_ACTION).strip().lower()
    if adapter_id != PLAYWRIGHT_ADAPTER_ID:
        raise ValueError(f"unsupported radar adapter: {adapter_id or '(missing)'}")
    if action != ALLOWED_ACTION:
        raise ValueError(f"unsupported radar adapter action: {action}")
    return {"adapter_id": adapter_id, "action": action}


def sanitized_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal child environment with credential-looking variables removed."""
    raw = dict(os.environ if source is None else source)
    clean: dict[str, str] = {}
    for key, value in raw.items():
        upper = key.upper()
        if any(marker in upper for marker in _SECRET_MARKERS):
            continue
        if key in _SAFE_ENV_KEYS or key.startswith("NPM_CONFIG_"):
            clean[key] = value
    clean["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    clean["FRISHTA_RADAR_ADAPTER"] = PLAYWRIGHT_ADAPTER_ID
    return clean


def find_system_browser() -> str | None:
    explicit = os.environ.get("CHROME_PATH", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    for candidate in _BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        resolved = shutil.which(binary)
        if resolved:
            return resolved
    return None


def _node_smoke_source() -> str:
    return r'''const fs = require("node:fs");
const crypto = require("node:crypto");
const { chromium } = require("playwright-core");

(async () => {
  const chromePath = process.argv[2];
  const screenshotPath = process.argv[3];
  const browser = await chromium.launch({ executablePath: chromePath, headless: true });
  try {
    const context = await browser.newContext({ offline: true });
    const page = await context.newPage();
    await page.setContent(`<!doctype html><html><body>
      <button id="run">Run Frishta Adapter</button>
      <output id="result">idle</output>
      <script>
        document.getElementById('run').addEventListener('click', () => {
          document.getElementById('result').textContent = 'FRISHTA_PLAYWRIGHT_OK';
        });
      </script>
    </body></html>`);
    await page.locator("#run").click();
    const value = await page.locator("#result").textContent();
    if (value !== "FRISHTA_PLAYWRIGHT_OK") throw new Error(`unexpected result: ${value}`);
    await page.screenshot({ path: screenshotPath, type: "png" });
    const screenshot = fs.readFileSync(screenshotPath);
    console.log(JSON.stringify({
      status: "PASS",
      browser_version: await browser.version(),
      local_page_interaction: true,
      offline_context: true,
      screenshot_sha256: crypto.createHash("sha256").update(screenshot).digest("hex")
    }));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
'''


def execute_playwright_smoke(work_root: Path) -> tuple[dict, bytes, str]:
    """Install the pinned package and run a local-page browser smoke with no target network."""
    browser = find_system_browser()
    if not browser:
        raise RuntimeError("system Chrome/Chromium not found on runner")

    work_root.mkdir(parents=True, exist_ok=True)
    env = sanitized_child_env()
    install = subprocess.run(
        [
            "npm",
            "install",
            "--no-save",
            "--ignore-scripts",
            "--package-lock=false",
            f"playwright-core@{PLAYWRIGHT_VERSION}",
        ],
        cwd=str(work_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    install_log = install.stdout + "\n" + install.stderr
    if install.returncode != 0:
        raise RuntimeError(f"playwright-core install failed: {install_log[-1200:]}")

    smoke_file = work_root / "smoke.cjs"
    screenshot_file = work_root / "playwright-smoke.png"
    smoke_file.write_text(_node_smoke_source(), encoding="utf-8")
    run = subprocess.run(
        ["node", str(smoke_file), browser, str(screenshot_file)],
        cwd=str(work_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if run.returncode != 0:
        raise RuntimeError(f"Playwright smoke failed: {(run.stdout + run.stderr)[-1600:]}")
    lines = [line.strip() for line in run.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("Playwright smoke returned no report")
    result = json.loads(lines[-1])
    if result.get("status") != "PASS":
        raise RuntimeError(f"unexpected Playwright report: {result}")
    if not screenshot_file.is_file() or screenshot_file.stat().st_size <= 0:
        raise RuntimeError("Playwright screenshot missing")
    return result, screenshot_file.read_bytes(), install_log


def run_radar_adapter_job(
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
    request = parse_adapter_request(str(context.get("goal") or ""))
    adapter_id = request["adapter_id"]

    update_job(
        state="RUNNING",
        log_append=f"[gha] radar adapter starting id={adapter_id}\n",
        checkpoint_stage="radar_adapter_prepare",
    )
    register_agent("RadarAdapterPlanner", "COMPLETE", f"adapter={adapter_id}; action=smoke; auto_integrate=false")

    work_root = Path(tempfile.mkdtemp(prefix="frishta-radar-adapter-"))
    try:
        update_job(
            state="TESTING",
            log_append="[gha] launching reviewed Playwright smoke in sanitized child env\n",
            checkpoint_stage="radar_adapter_smoke",
        )
        result, screenshot, install_log = execute_playwright_smoke(work_root)
    except Exception as exc:  # noqa: BLE001
        register_agent("RadarAdapterRunner", "FAILED", str(exc)[:500])
        update_job(
            state="FAILED",
            failure_reason="radar_adapter_smoke_failed",
            result_summary=f"Radar adapter failed safely: {exc}",
            log_append=f"[gha] radar adapter FAILED: {exc}\n",
        )
        raise

    report = {
        "schema_version": 1,
        "job_id": job_id,
        "project_id": project_id,
        "github_run_id": github_run_id,
        "adapter_id": adapter_id,
        "adapter_action": request["action"],
        "tool": "playwright-core",
        "tool_version": PLAYWRIGHT_VERSION,
        "source_repository": PLAYWRIGHT_REPOSITORY,
        "executor": "GITHUB_ACTIONS_EPHEMERAL",
        "status": "PASS",
        "browser_version": result.get("browser_version"),
        "local_page_interaction": bool(result.get("local_page_interaction")),
        "offline_browser_context": bool(result.get("offline_context")),
        "screenshot_sha256": result.get("screenshot_sha256"),
        "untrusted_repository_code_executed": False,
        "source_repository_cloned": False,
        "adapter_child_received_secrets": False,
        "orchestrator_uses_callback_secret": True,
        "external_browser_targets": 0,
        "auto_integrate": False,
    }
    stage_artifact(
        "radar-adapter-report.json",
        "application/json",
        json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8"),
    )
    stage_artifact("radar-adapter-screenshot.png", "image/png", screenshot)
    stage_artifact("radar-adapter-install-log.txt", "text/plain", install_log[-10000:].encode("utf-8"))
    register_agent(
        "RadarAdapterRunner",
        "COMPLETE",
        f"{adapter_id} PASS; browser={report['browser_version']}; offline=true; no untrusted repo code",
    )
    update_job(
        state="VERIFYING",
        result_summary=(
            f"Radar adapter {adapter_id} passed in ephemeral cloud runner; "
            "local-page browser automation verified; no auto-integration"
        ),
        log_append="[gha] radar adapter evidence staged\n",
        checkpoint_stage="radar_adapter_artifact_upload",
    )
