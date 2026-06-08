"""Background-friendly memory curation service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.memory import MemoryItem
from agent_kernel.memory import MemoryEvolutionSettlement, MemoryEvolutionSettlementService, MemoryFacade


@dataclass(slots=True)
class MemoryCurationReport:
  run_id: str
  scope: str
  episodic_memory_id: str | None = None
  settlements: list[MemoryEvolutionSettlement] = field(default_factory=list)


class MemoryCurator:
  """Deterministically distills run events into episodic/settled memory."""

  INTERESTING_EVENT_TYPES = {
    RuntimeEventType.TOOL_CALL_COMPLETED,
    RuntimeEventType.AGENT_TURN_COMPLETED,
    RuntimeEventType.AGENT_DELEGATION_COMPLETED,
    RuntimeEventType.WORKBENCH_UPDATED,
    RuntimeEventType.WORKBENCH_CREATED,
    RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE,
  }

  def __init__(
    self,
    uow_factory,
    *,
    memory: MemoryFacade | None = None,
    settlement_service: MemoryEvolutionSettlementService | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._memory = memory or MemoryFacade(uow_factory)
    self._settlement = settlement_service or MemoryEvolutionSettlementService(uow_factory, self._memory)

  def curate_run(self, run_id: str, *, scope: str | None = None) -> MemoryCurationReport:
    if not run_id:
      raise ValueError("run_id is required.")
    target_scope = scope or run_id
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id)
    interesting = [event for event in events if event.event_type in self.INTERESTING_EVENT_TYPES]
    episodic = None
    if interesting and not self._already_curated(target_scope, run_id):
      episodic = self._write_episode(target_scope, run_id, interesting)
    settlements = self._settlement.settle_run(run_id, scope=target_scope)
    return MemoryCurationReport(
      run_id=run_id,
      scope=target_scope,
      episodic_memory_id=episodic.memory_id if episodic is not None else None,
      settlements=settlements,
    )

  def _write_episode(self, scope: str, run_id: str, events: list[RuntimeEvent]) -> MemoryItem:
    artifact_refs = _unique_artifact_refs(ref for event in events for ref in event.artifact_refs)
    item = self._memory.write_episode(
      scope=scope,
      task_id=events[-1].task_id,
      event_ids=[event.event_id for event in events],
      observations=[_event_observation(event) for event in events],
      outcome=_run_outcome(events),
      artifact_refs=artifact_refs,
      importance=0.72,
      created_by="memory_curator",
    )
    item.content = {
      **item.content,
      "kind": "run_event_summary",
      "source_run_id": run_id,
    }
    self._memory.save(item)
    return item

  def _already_curated(self, scope: str, run_id: str) -> bool:
    for item in self._memory.retrieve(scope, memory_type="episodic", limit=200):
      if item.content.get("kind") == "run_event_summary" and item.content.get("source_run_id") == run_id:
        return True
    return False


def _event_observation(event: RuntimeEvent) -> str:
  payload = event.payload
  summary = payload.get("summary") or payload.get("content") or payload.get("output") or payload
  return f"{event.event_type.value}: {_compact(str(summary), 600)}"


def _run_outcome(events: list[RuntimeEvent]) -> str:
  last = events[-1]
  return f"Curated {len(events)} run events. Last event: {last.event_type.value}."


def _unique_artifact_refs(refs) -> list[ArtifactRef]:
  unique: dict[str, ArtifactRef] = {}
  for ref in refs:
    unique.setdefault(ref.artifact_id, ref)
  return list(unique.values())


def _compact(value: str, limit: int) -> str:
  return value if len(value) <= limit else value[:limit] + "...[truncated]"
