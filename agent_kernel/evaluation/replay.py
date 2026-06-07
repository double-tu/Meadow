"""Exact replay from persisted events and checkpoints."""

from dataclasses import dataclass, field

from agent_kernel.domain.base import DomainModel
from agent_kernel.domain.events import RuntimeEvent
from agent_kernel.domain.run import RunState


@dataclass(slots=True)
class ReplayResult(DomainModel):
  run_id: str
  final_state: RunState | None
  events: list[RuntimeEvent] = field(default_factory=list)
  replayed_external_calls: int = 0


class ReplayService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def exact_replay(self, run_id: str) -> ReplayResult:
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id)
      checkpoint = uow.checkpoints.latest_for_run(run_id)
    return ReplayResult(
      run_id=run_id,
      final_state=checkpoint.state if checkpoint is not None else None,
      events=events,
      replayed_external_calls=0,
    )

  def partial_replay(self, run_id: str, until_event_id: str) -> ReplayResult:
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id)
    selected: list[RuntimeEvent] = []
    for event in events:
      selected.append(event)
      if event.event_id == until_event_id:
        break
    return ReplayResult(
      run_id=run_id,
      final_state=None,
      events=selected,
      replayed_external_calls=0,
    )

  def recovery_replay(self, run_id: str) -> ReplayResult:
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id)
      checkpoint = uow.checkpoints.latest_for_run(run_id)
    if checkpoint is None or checkpoint.event_id is None:
      replay_events = events
    else:
      seen_checkpoint_event = False
      replay_events = []
      for event in events:
        if seen_checkpoint_event:
          replay_events.append(event)
        if event.event_id == checkpoint.event_id:
          seen_checkpoint_event = True
    return ReplayResult(
      run_id=run_id,
      final_state=checkpoint.state if checkpoint is not None else None,
      events=replay_events,
      replayed_external_calls=0,
    )
