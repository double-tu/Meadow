"""Auxiliary observer service."""

from agent_kernel.domain.base import new_id
from agent_kernel.domain.interaction import ObservationFinding


class ObserverService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

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
    return finding

