"""Auxiliary observer service."""

from typing import Protocol

from agent_kernel.domain.base import new_id
from agent_kernel.domain.interaction import ObservationFinding
from agent_kernel.domain.run import RunState


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
  def __init__(self, uow_factory, pause_controller: RunPauseController | None = None) -> None:
    self._uow_factory = uow_factory
    self._pause_controller = pause_controller

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
