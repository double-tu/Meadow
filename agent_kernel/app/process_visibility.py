"""Process visibility DTOs for UI/workbench surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_kernel.domain.events import RuntimeEvent


@dataclass(slots=True)
class ProcessActivity:
  activity_id: str
  kind: str
  title: str
  status: str
  run_id: str
  agent_id: str | None = None
  task_id: str | None = None
  summary: str | None = None
  payload: dict[str, Any] = field(default_factory=dict)

  def to_dict(self) -> dict[str, Any]:
    return {
      "activity_id": self.activity_id,
      "kind": self.kind,
      "title": self.title,
      "status": self.status,
      "run_id": self.run_id,
      "agent_id": self.agent_id,
      "task_id": self.task_id,
      "summary": self.summary,
      "payload": self.payload,
    }


class ProcessVisibilityProjector:
  """Projects runtime events into UI-friendly activity rows."""

  def from_events(self, events: list[RuntimeEvent]) -> list[ProcessActivity]:
    activities: list[ProcessActivity] = []
    for event in events:
      kind = event.event_type.value
      if kind.startswith("tool.call"):
        activities.append(self._activity(event, "tool_call", "Tool call", self._status(kind)))
      elif kind.startswith("agent.delegation"):
        activities.append(self._activity(event, "agent_delegation", "Agent delegation", self._status(kind)))
      elif kind.startswith("workbench"):
        activities.append(self._activity(event, "workbench", "Workbench", self._status(kind)))
      elif kind.startswith("approval"):
        activities.append(self._activity(event, "approval", "Approval", self._status(kind)))
      elif kind == "context.built":
        activities.append(self._activity(event, "context", "Context built", "completed"))
    return activities

  @staticmethod
  def _activity(event: RuntimeEvent, kind: str, title: str, status: str) -> ProcessActivity:
    summary = event.payload.get("summary")
    if not isinstance(summary, str):
      summary = event.payload.get("reason") if isinstance(event.payload.get("reason"), str) else None
    return ProcessActivity(
      activity_id=event.event_id,
      kind=kind,
      title=title,
      status=status,
      run_id=event.run_id,
      agent_id=event.agent_id,
      task_id=event.task_id,
      summary=summary,
      payload=event.payload,
    )

  @staticmethod
  def _status(event_type: str) -> str:
    if event_type.endswith(".started") or event_type.endswith(".requested"):
      return "running"
    if event_type.endswith(".completed") or event_type.endswith(".resolved") or event_type.endswith(".created"):
      return "completed"
    if event_type.endswith(".failed"):
      return "failed"
    if event_type.endswith(".cancelled") or event_type.endswith(".killed"):
      return "cancelled"
    return "unknown"
