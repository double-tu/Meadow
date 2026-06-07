"""Trace timeline aggregation."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_kernel.domain.base import DomainModel


@dataclass(slots=True)
class TimelineEntry(DomainModel):
  kind: str
  timestamp: datetime
  ref_id: str
  summary: str
  payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TraceTimeline(DomainModel):
  run_id: str
  entries: list[TimelineEntry]


class TraceService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def build_timeline(self, run_id: str) -> TraceTimeline:
    entries: list[TimelineEntry] = []
    with self._uow_factory() as uow:
      for event in uow.events.list_by_run(run_id):
        entries.append(
          TimelineEntry(
            kind="event",
            timestamp=event.timestamp,
            ref_id=event.event_id,
            summary=str(event.event_type.value),
            payload=event.payload,
          )
        )
      for step in uow.steps.list_by_run(run_id):
        entries.append(
          TimelineEntry(
            kind="step",
            timestamp=step.updated_at,
            ref_id=step.step_id,
            summary=f"{step.node_id}:{step.status.value}",
            payload={"node_id": step.node_id, "attempt": step.attempt},
          )
        )
      for call in uow.tool_calls.list_by_run(run_id):
        entries.append(
          TimelineEntry(
            kind="tool_call",
            timestamp=call.updated_at,
            ref_id=call.tool_call_id,
            summary=f"{call.capability_id}:{call.status.value}",
            payload={"capability_id": call.capability_id, "error": call.error},
          )
        )
      for audit in uow.audit.list_by_run(run_id):
        entries.append(
          TimelineEntry(
            kind="audit",
            timestamp=audit.created_at,
            ref_id=audit.audit_id,
            summary=f"{audit.action}:{audit.decision}",
            payload=audit.payload,
          )
        )
    entries.sort(key=lambda entry: (entry.timestamp, entry.kind, entry.ref_id))
    return TraceTimeline(run_id=run_id, entries=entries)

