"""Exploration attempt executor."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from agent_kernel.domain.autonomy import AttemptStatus, CandidateStrategy, ExplorationAttempt
from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.autonomy.verifier import ExplorationVerifier


AttemptExecutor = Callable[[CandidateStrategy], dict[str, Any]]


class ExplorationExecutor:
  def __init__(self, uow_factory, attempt_executor: AttemptExecutor, verifier: ExplorationVerifier) -> None:
    self._uow_factory = uow_factory
    self._attempt_executor = attempt_executor
    self._verifier = verifier

  def execute(self, strategy: CandidateStrategy) -> ExplorationAttempt:
    attempt = ExplorationAttempt(
      attempt_id=new_id("attempt"),
      exploration_id=strategy.exploration_id,
      strategy_id=strategy.strategy_id,
      run_id=new_id("run"),
      status=AttemptStatus.RUNNING,
      started_at=utc_now(),
    )
    with self._uow_factory() as uow:
      uow.autonomy.save_attempt(attempt)
    raw_result = self._attempt_executor(strategy)
    verification = self._verifier.verify(raw_result)
    completed = replace(
      attempt,
      status=AttemptStatus.SUCCEEDED if verification.ok else AttemptStatus.FAILED,
      result_summary=verification.summary,
      failure_reason=None if verification.ok else verification.summary,
      event_refs=list(raw_result.get("event_refs", [])),
      completed_at=utc_now(),
    )
    with self._uow_factory() as uow:
      uow.autonomy.save_attempt(completed)
    return completed

