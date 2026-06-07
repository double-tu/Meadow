"""Exploration failure reflection."""

from __future__ import annotations

from typing import Protocol

from agent_kernel.domain.autonomy import (
  AttemptStatus,
  CandidateStrategy,
  ExplorationAttempt,
  ExplorationTask,
  ReflectionRecord,
  ReflectionStatus,
)
from agent_kernel.domain.base import new_id


class ExplorationReflector(Protocol):
  def reflect(
    self,
    task: ExplorationTask,
    strategy: CandidateStrategy,
    attempt: ExplorationAttempt,
  ) -> ReflectionRecord:
    ...


class DeterministicExplorationReflector:
  """Rule-based reflector that turns failed attempts into reusable strategy hints."""

  def reflect(
    self,
    task: ExplorationTask,
    strategy: CandidateStrategy,
    attempt: ExplorationAttempt,
  ) -> ReflectionRecord:
    failure_summary = attempt.failure_reason or attempt.result_summary or "attempt failed"
    root_causes = self._root_causes(failure_summary)
    return ReflectionRecord(
      reflection_id=new_id("reflection"),
      exploration_id=task.exploration_id,
      attempt_id=attempt.attempt_id,
      status=ReflectionStatus.PROPOSED,
      failure_summary=failure_summary,
      root_causes=root_causes,
      avoid_patterns=self._avoid_patterns(strategy, root_causes),
      next_strategy_hints=self._next_strategy_hints(task, strategy, attempt, root_causes),
    )

  @staticmethod
  def _root_causes(summary: str) -> list[str]:
    text = summary.lower()
    causes: list[str] = []
    if "timeout" in text or "timed out" in text:
      causes.append("timeout")
    if "permission" in text or "denied" in text or "approval" in text:
      causes.append("permission")
    if "missing" in text or "not found" in text:
      causes.append("missing_dependency")
    if "invalid" in text or "schema" in text or "parse" in text:
      causes.append("invalid_input")
    if not causes:
      causes.append("verification_failed")
    return causes

  @staticmethod
  def _avoid_patterns(strategy: CandidateStrategy, root_causes: list[str]) -> list[str]:
    patterns = [f"repeat strategy without changes: {strategy.strategy_id}"]
    if "timeout" in root_causes:
      patterns.append("retrying the same long-running path without smaller checkpoints")
    if "permission" in root_causes:
      patterns.append("executing side-effecting tools before grant/approval planning")
    if "missing_dependency" in root_causes:
      patterns.append("assuming unavailable tools, files, or services are present")
    return patterns

  @staticmethod
  def _next_strategy_hints(
    task: ExplorationTask,
    strategy: CandidateStrategy,
    attempt: ExplorationAttempt,
    root_causes: list[str],
  ) -> list[str]:
    hints = [
      f"Revise hypothesis for objective {task.objective_id}: {strategy.hypothesis}",
      f"Use evidence from failed run {attempt.run_id} before launching another attempt.",
    ]
    if "timeout" in root_causes:
      hints.append("Split the next attempt into smaller verifiable steps with a lower-cost probe first.")
    if "permission" in root_causes:
      hints.append("Request or declare required capability grants before executing the next attempt.")
    if "missing_dependency" in root_causes:
      hints.append("Add a discovery step to verify required tools, files, and services.")
    if "invalid_input" in root_causes:
      hints.append("Validate input schemas and command arguments before execution.")
    if attempt.status is AttemptStatus.FAILED:
      hints.append("Try a different tool/workflow combination instead of only rerunning.")
    return hints
