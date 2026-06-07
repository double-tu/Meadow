"""Persistent recovery scanner for stale runtime work."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.run import RunState
from agent_kernel.domain.states import NodeStepStatus, RecoveryStatus
from agent_kernel.domain.step import NodeStepRecord
from agent_kernel.domain.stability import RecoveryJob
from agent_kernel.persistence.unit_of_work import UnitOfWork
from agent_kernel.runtime.dead_letter import build_dead_letter


RECOVERABLE_STEP_STATUSES = {
  NodeStepStatus.LEASED,
  NodeStepStatus.RUNNING,
  NodeStepStatus.RETRY_WAIT,
}


@dataclass(slots=True)
class RecoveryScanResult:
  scanned_steps: int = 0
  recovered_steps: int = 0
  dead_lettered_steps: int = 0
  jobs: list[RecoveryJob] | None = None


@dataclass(slots=True)
class ConsistencyIssue:
  issue_type: Literal[
    "missing_run_state",
    "checkpoint_event_missing",
    "state_checkpoint_missing",
    "state_checkpoint_mismatch",
    "artifact_ref_missing",
  ]
  run_id: str
  target_type: str
  target_id: str
  message: str


@dataclass(slots=True)
class ConsistencyCheckResult:
  checked_runs: int = 0
  issues: list[ConsistencyIssue] | None = None
  jobs: list[RecoveryJob] | None = None


class RecoveryScanner:
  def __init__(
    self,
    uow_factory,
    stale_after_seconds: int = 300,
    max_recovery_attempts: int = 3,
  ) -> None:
    self._uow_factory = uow_factory
    self._stale_after_seconds = stale_after_seconds
    self._max_recovery_attempts = max_recovery_attempts

  def scan(self, now: datetime | None = None) -> RecoveryScanResult:
    now = now or utc_now()
    cutoff = now - timedelta(seconds=self._stale_after_seconds)
    jobs: list[RecoveryJob] = []
    scanned = recovered = dead_lettered = 0

    with self._uow_factory() as uow:
      stale_steps = [
        step
        for step in uow.steps.list_recoverable()
        if step.updated_at <= cutoff
      ]
      for step in stale_steps:
        scanned += 1
        job = self._recover_step(uow, step)
        jobs.append(job)
        if job.status is RecoveryStatus.SUCCEEDED:
          recovered += 1
        elif job.status is RecoveryStatus.DEAD_LETTERED:
          dead_lettered += 1

    return RecoveryScanResult(
      scanned_steps=scanned,
      recovered_steps=recovered,
      dead_lettered_steps=dead_lettered,
      jobs=jobs,
    )

  def check_consistency(self, run_id: str | None = None) -> ConsistencyCheckResult:
    issues: list[ConsistencyIssue] = []
    jobs: list[RecoveryJob] = []
    checked_runs: set[str] = set()

    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id) if run_id else uow.events.list_all()
      run_ids = {event.run_id for event in events}
      if run_id:
        run_ids.add(run_id)
      run_ids.update(state.run_id for state in uow.states.list_all())

      for current_run_id in sorted(run_ids):
        checked_runs.add(current_run_id)
        state = uow.states.get(current_run_id)
        checkpoints = uow.checkpoints.list_by_run(current_run_id)
        run_events = [event for event in events if event.run_id == current_run_id]

        if run_events and state is None:
          issues.append(
            ConsistencyIssue(
              issue_type="missing_run_state",
              run_id=current_run_id,
              target_type="run",
              target_id=current_run_id,
              message="Run has events but no persisted RunState.",
            )
          )

        for checkpoint in checkpoints:
          if checkpoint.event_id and uow.events.get(checkpoint.event_id) is None:
            issues.append(
              ConsistencyIssue(
                issue_type="checkpoint_event_missing",
                run_id=current_run_id,
                target_type="checkpoint",
                target_id=checkpoint.checkpoint_id,
                message=f"Checkpoint references missing event {checkpoint.event_id}.",
              )
            )

        if state is not None:
          issues.extend(self._state_checkpoint_issues(uow, state))
          issues.extend(self._artifact_ref_issues(uow, state.artifact_refs, current_run_id, "run_state", state.run_id))

        for event in run_events:
          issues.extend(
            self._artifact_ref_issues(
              uow,
              event.artifact_refs,
              current_run_id,
              "runtime_event",
              event.event_id,
            )
          )

      for issue in issues:
        jobs.append(self._record_consistency_issue(uow, issue))

    return ConsistencyCheckResult(
      checked_runs=len(checked_runs),
      issues=issues,
      jobs=jobs,
    )

  def _recover_step(self, uow: UnitOfWork, step: NodeStepRecord) -> RecoveryJob:
    reason = f"stale step detected in status {step.status.value}"
    checkpoint = uow.checkpoints.latest_for_run(step.run_id)
    job = RecoveryJob(
      recovery_id=new_id("recovery"),
      target_type="node_step",
      target_id=step.step_id,
      status=RecoveryStatus.RUNNING,
      reason=reason,
      last_checkpoint_id=checkpoint.checkpoint_id if checkpoint else None,
      attempt=step.attempt + 1,
    )
    uow.recovery.save(job)

    if step.attempt >= self._max_recovery_attempts:
      event = RuntimeEvent(
        event_type=RuntimeEventType.RECOVERY_FAILED,
        run_id=step.run_id,
        node_id=step.node_id,
        step_id=step.step_id,
        payload={"reason": reason, "action": "dead_letter", "attempt": step.attempt},
      )
      uow.events.append(event)
      uow.dead_letters.add(
        build_dead_letter(
          target_type="node_step",
          target_id=step.step_id,
          reason=f"{reason}; max recovery attempts exhausted",
          failure_type="recovery_exhausted",
          retryable=False,
          event_refs=[event.event_id],
        )
      )
      failed_step = replace(
        step,
        status=NodeStepStatus.FAILED,
        error="recovery attempts exhausted",
        updated_at=utc_now(),
      )
      uow.steps.save(failed_step)
      dead_lettered = replace(job, status=RecoveryStatus.DEAD_LETTERED, updated_at=utc_now())
      uow.recovery.save(dead_lettered)
      return dead_lettered

    rescheduled = replace(
      step,
      status=NodeStepStatus.SCHEDULED,
      lease_id=None,
      error=reason,
      updated_at=utc_now(),
    )
    uow.steps.save(rescheduled)
    uow.events.append(
      RuntimeEvent(
        event_type=RuntimeEventType.RECOVERY_SUCCEEDED,
        run_id=step.run_id,
        node_id=step.node_id,
        step_id=step.step_id,
        payload={
          "reason": reason,
          "action": "reschedule_step",
          "attempt": step.attempt,
          "next_status": NodeStepStatus.SCHEDULED.value,
        },
      )
    )
    succeeded = replace(job, status=RecoveryStatus.SUCCEEDED, updated_at=utc_now())
    uow.recovery.save(succeeded)
    return succeeded

  def _state_checkpoint_issues(self, uow: UnitOfWork, state: RunState) -> list[ConsistencyIssue]:
    if state.checkpoint_id is None:
      return []
    latest = uow.checkpoints.latest_for_run(state.run_id)
    if latest is None:
      return [
        ConsistencyIssue(
          issue_type="state_checkpoint_missing",
          run_id=state.run_id,
          target_type="run_state",
          target_id=state.run_id,
          message=f"RunState references checkpoint {state.checkpoint_id}, but run has no checkpoints.",
        )
      ]
    if latest.checkpoint_id != state.checkpoint_id:
      return [
        ConsistencyIssue(
          issue_type="state_checkpoint_mismatch",
          run_id=state.run_id,
          target_type="run_state",
          target_id=state.run_id,
          message=(
            f"RunState checkpoint {state.checkpoint_id} does not match latest "
            f"checkpoint {latest.checkpoint_id}."
          ),
        )
      ]
    return []

  def _artifact_ref_issues(
    self,
    uow: UnitOfWork,
    refs: list[ArtifactRef],
    run_id: str,
    target_type: str,
    target_id: str,
  ) -> list[ConsistencyIssue]:
    issues: list[ConsistencyIssue] = []
    for ref in refs:
      if uow.artifacts.get(ref.artifact_id) is None:
        issues.append(
          ConsistencyIssue(
            issue_type="artifact_ref_missing",
            run_id=run_id,
            target_type=target_type,
            target_id=target_id,
            message=f"Missing artifact metadata for ref {ref.artifact_id}.",
          )
        )
    return issues

  def _record_consistency_issue(
    self,
    uow: UnitOfWork,
    issue: ConsistencyIssue,
  ) -> RecoveryJob:
    event = RuntimeEvent(
      event_type=RuntimeEventType.RECOVERY_FAILED,
      run_id=issue.run_id,
      payload={
        "reason": issue.message,
        "action": "consistency_check",
        "issue_type": issue.issue_type,
        "target_type": issue.target_type,
        "target_id": issue.target_id,
      },
    )
    uow.events.append(event)
    job = RecoveryJob(
      recovery_id=new_id("recovery"),
      target_type="run" if issue.target_type in {"run", "run_state", "checkpoint"} else "node_step",
      target_id=issue.target_id,
      status=RecoveryStatus.FAILED,
      reason=issue.message,
      replay_from_event_id=event.event_id,
    )
    uow.recovery.save(job)
    return job
