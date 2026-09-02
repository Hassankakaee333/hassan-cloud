"""Multi-agent MVP: Planner → Worker → Reviewer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AgentRunResult:
    role: str
    output: str


def run_pipeline(goal: str) -> list[AgentRunResult]:
  planner = AgentRunResult(
      role="planner",
      output=(
          f"خطة للهدف: {goal}\n"
          "1) فهم المتطلبات\n"
          "2) إنشاء workspace معزول\n"
          "3) تطبيق التغييرات\n"
          "4) تشغيل الاختبارات\n"
          "5) مراجعة مستقلة\n"
          "6) إرجاع artifacts"
      ),
  )
  worker = AgentRunResult(
      role="worker",
      output=(
          "تمت محاكاة خطوة التنفيذ في workspace معزول. "
          "في الإصدار الكامل سيتم تعديل الملفات وتشغيل build/test فعلياً."
      ),
  )
  reviewer = AgentRunResult(
      role="reviewer",
      output=(
          "مراجعة مستقلة: الخطة منطقية لكن التنفيذ الفعلي يتطلب workspace مُفعّل. "
          "الحالة: PARTIAL — foundation جاهز."
      ),
  )
  return [planner, worker, reviewer]
