"""Auxiliary observer service."""

from typing import Protocol

from agent_kernel.domain.base import new_id
from agent_kernel.domain.interaction import ObservationFinding
from agent_kernel.domain.memory import MemoryItem
from agent_kernel.domain.run import RunState
from agent_kernel.memory import MemoryFacade
from agent_kernel.policy.intervention import CurrentStepInterrupter, PersistenceCurrentStepInterrupter


class RunPauseController(Protocol):
  def pause_run(
    self,
    run_id: str,
    reason: str,
    *,
    source: str = "runtime",
    causal_id: str | None = None,
    payload: dict[str, object] | None = None,
  ) -> RunState:
    ...


class ObserverService:
  def __init__(
    self,
    uow_factory,
    pause_controller: RunPauseController | None = None,
    memory: MemoryFacade | None = None,
    step_interrupter: CurrentStepInterrupter | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._pause_controller = pause_controller
    self._memory = memory or MemoryFacade(uow_factory)
    self._step_interrupter = step_interrupter or PersistenceCurrentStepInterrupter(uow_factory)

  def request_pause(self, observer_id: str, target_run_id: str, message: str) -> ObservationFinding:
    finding = ObservationFinding(
      finding_id=new_id("finding"),
      observer_id=observer_id,
      target_run_id=target_run_id,
      severity="critical",
      action="request_pause",
      message=message,
    )
    with self._uow_factory() as uow:
      uow.interactions.save_finding(finding)
    if self._pause_controller is not None:
      self._pause_controller.pause_run(
        target_run_id,
        message,
        source="observer",
        causal_id=finding.finding_id,
        payload={"observer_id": observer_id, "finding_id": finding.finding_id},
      )
    return finding

  def request_context_correction(
    self,
    observer_id: str,
    target_run_id: str,
    message: str,
    *,
    memory_scope: str | None = None,
    severity: str = "warning",
    evidence_event_refs: list[str] | None = None,
  ) -> tuple[ObservationFinding, MemoryItem]:
    finding = ObservationFinding(
      finding_id=new_id("finding"),
      observer_id=observer_id,
      target_run_id=target_run_id,
      severity=severity,  # type: ignore[arg-type]
      action="intervene",
      message=message,
      evidence_event_refs=evidence_event_refs or [],
    )
    memory = self._memory.write_working(
      scope=memory_scope or target_run_id,
      content={
        "kind": "observer_context_correction",
        "finding_id": finding.finding_id,
        "observer_id": observer_id,
        "target_run_id": target_run_id,
        "severity": severity,
        "content": message,
        "evidence_event_refs": evidence_event_refs or [],
      },
      importance=0.95,
      created_by=f"observer:{observer_id}",
    )
    with self._uow_factory() as uow:
      uow.interactions.save_finding(finding)
    return finding, memory

  def request_step_interrupt(
    self,
    observer_id: str,
    target_run_id: str,
    message: str,
    *,
    evidence_event_refs: list[str] | None = None,
  ) -> tuple[ObservationFinding, str | None]:
    finding = ObservationFinding(
      finding_id=new_id("finding"),
      observer_id=observer_id,
      target_run_id=target_run_id,
      severity="critical",
      action="intervene",
      message=message,
      evidence_event_refs=evidence_event_refs or [],
    )
    with self._uow_factory() as uow:
      uow.interactions.save_finding(finding)
    interrupted_step_id = self._step_interrupter.interrupt_current_step(
      target_run_id,
      message,
      causal_id=finding.finding_id,
      source="observer",
    )
    return finding, interrupted_step_id
