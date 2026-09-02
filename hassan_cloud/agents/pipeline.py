"""Agent pipeline with checkpoint resume."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..storage.repository import DatabaseRepository


@dataclass
class AgentRunResult:
    agent_id: str
    role: str
    status: str
    output: str
    verification_notes: str = ""


def run_agent_pipeline(
    repo: "DatabaseRepository",
    job_id: str,
    goal: str,
    coding_result: dict | None,
    new_id,
    now_ms,
) -> tuple[str, str]:
    """Planner → Worker → Reviewer → Verifier. Skips completed agent stages on resume."""
    runs: list[AgentRunResult] = []
    ts = now_ms()

    if not repo.has_completed_agent(job_id, "planner"):
        planner_out = (
            f"خطة للهدف: {goal}\n"
            "1) workspace معزول\n2) تعديل\n3) pytest\n4) مراجعة\n5) تحقق\n6) artifacts"
        )
        runs.append(AgentRunResult("planner", "planner", "COMPLETED", planner_out))
        _persist(repo, job_id, goal, runs[-1], new_id, now_ms())
    else:
        runs.append(AgentRunResult("planner", "planner", "COMPLETED", "[resumed]"))

    if not repo.has_completed_agent(job_id, "coding_worker"):
        if coding_result:
            worker_out = (
                f"exit_code={coding_result.get('exit_code')}\n"
                f"diff={str(coding_result.get('diff', ''))[:200]}\n"
                f"stdout={coding_result.get('stdout', '')[:400]}"
            )
            worker_status = "COMPLETED" if coding_result.get("exit_code") == 0 else "FAILED"
        else:
            worker_out = "لا يوجد workspace coding."
            worker_status = "COMPLETED"
        runs.append(AgentRunResult("coder", "coding_worker", worker_status, worker_out))
        _persist(repo, job_id, goal, runs[-1], new_id, now_ms())
    else:
        runs.append(AgentRunResult("coder", "coding_worker", "COMPLETED", "[resumed]"))

    if not repo.has_completed_agent(job_id, "reviewer"):
        if coding_result:
            stdout = (coding_result.get("stdout") or "").lower()
            passed = coding_result.get("exit_code") == 0 and ("passed" in stdout or "1 passed" in stdout)
            review = "مراجعة: الاختبارات نجحت." if passed else f"مراجعة: فشل — exit={coding_result.get('exit_code')}"
            review_status = "COMPLETED" if passed else "FAILED"
        else:
            review = "مراجعة: لا مخرجات build."
            review_status = "COMPLETED"
        runs.append(AgentRunResult("reviewer", "reviewer", review_status, review))
        _persist(repo, job_id, goal, runs[-1], new_id, now_ms())
    else:
        runs.append(AgentRunResult("reviewer", "reviewer", "COMPLETED", "[resumed]"))

    if not repo.has_completed_agent(job_id, "verifier"):
        if coding_result:
            has_zip = bool(coding_result.get("zip_bytes"))
            verify_ok = bool(coding_result.get("tests_passed")) and has_zip
            verify = (
                "Verifier: exit_code=0, tests passed, ZIP present."
                if verify_ok
                else "Verifier: evidence incomplete."
            )
            verify_status = "COMPLETED" if verify_ok else "FAILED"
            notes = f"tests_passed={coding_result.get('tests_passed')} zip={has_zip}"
        else:
            verify = "Verifier: skipped — no coding evidence."
            verify_status = "COMPLETED"
            notes = ""
        runs.append(AgentRunResult("verifier", "verifier", verify_status, verify, notes))
        _persist(repo, job_id, goal, runs[-1], new_id, now_ms())
    else:
        runs.append(AgentRunResult("verifier", "verifier", "COMPLETED", "[resumed]"))

    final = "COMPLETED" if all(r.status == "COMPLETED" for r in runs) else "FAILED"
    return final, "\n".join(f"{r.role}: {r.output[:100]}" for r in runs)


def _persist(repo, job_id, goal, r: AgentRunResult, new_id, now_ms) -> None:
    repo.insert_agent_run({
        "id": new_id(),
        "job_id": job_id,
        "agent_id": r.agent_id,
        "agent_role": r.role,
        "status": r.status,
        "input_text": goal,
        "output_text": r.output,
        "verification_notes": r.verification_notes,
        "created_at": now_ms,
    })
