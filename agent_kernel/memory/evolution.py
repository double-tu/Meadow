"""Memory evolution settlement service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.memory import MemoryItem
from agent_kernel.memory.facade import MemoryFacade


MemoryEvolutionTarget = Literal["semantic", "procedural"]


@dataclass(slots=True)
class MemoryEvolutionSettlement:
  candidate_id: str
  source_event_id: str
  memory_id: str
  memory_type: MemoryEvolutionTarget
  scope: str
  decision: str = "settled"


class MemoryEvolutionSettlementService:
  """Settles candidate notes into durable memory records.

  This intentionally keeps the first settlement mechanism deterministic. A
  later learned/LLM-based strategy can replace `_target_memory_type` and content
  shaping without changing the event, memory, or CLI boundaries.
  """

  def __init__(self, uow_factory, memory: MemoryFacade | None = None) -> None:
    self._uow_factory = uow_factory
    self._memory = memory or MemoryFacade(uow_factory)

  def settle_run(
    self,
    run_id: str,
    *,
    scope: str | None = None,
  ) -> list[MemoryEvolutionSettlement]:
    if not run_id:
      raise ValueError("run_id is required.")
    with self._uow_factory() as uow:
      events = uow.events.list_by_run(run_id)
    settlements: list[MemoryEvolutionSettlement] = []
    for event in events:
      if event.event_type is not RuntimeEventType.MEMORY_EVOLUTION_CANDIDATE:
        continue
      if not self._has_evidence(event.payload):
        continue
      candidate_scope = self._candidate_scope(event, run_id=run_id, override_scope=scope)
      if self._already_settled(candidate_scope, event.event_id):
        continue
      memory_type = self._target_memory_type(event.payload)
      item = self._write_memory(event, candidate_scope, memory_type)
      self._append_memory_write_event(run_id, event, item)
      settlements.append(
        MemoryEvolutionSettlement(
          candidate_id=str(event.payload.get("candidate_id") or event.event_id),
          source_event_id=event.event_id,
          memory_id=item.memory_id,
          memory_type=memory_type,
          scope=candidate_scope,
        )
      )
    return settlements

  def _write_memory(
    self,
    event: RuntimeEvent,
    scope: str,
    memory_type: MemoryEvolutionTarget,
  ) -> MemoryItem:
    note = event.payload.get("note")
    if not isinstance(note, str) or not note.strip():
      raise ValueError(f"Memory evolution candidate missing note: {event.event_id}")
    content = {
      "kind": "memory_evolution_settlement",
      "note": note,
      "evidence_summary": event.payload.get("evidence_summary"),
      "source_tool_call_ids": _string_list(event.payload.get("source_tool_call_ids")),
      "source_event_ids": _string_list(event.payload.get("source_event_ids")),
      "artifact_ids": _string_list(event.payload.get("artifact_ids")),
      "candidate_id": event.payload.get("candidate_id"),
      "source_run_id": event.run_id,
      "source": event.payload.get("source", "memory_evolution_candidate"),
    }
    if memory_type == "procedural":
      return self._memory.write(
        memory_type="procedural",
        scope=scope,
        content=content,
        importance=float(event.payload.get("importance", 0.8)),
        confidence=float(event.payload.get("confidence", 0.7)),
        created_by="memory_evolution_settlement",
        source_event_ids=[event.event_id],
      )
    return self._memory.write_semantic(
      scope=scope,
      content=content,
      importance=float(event.payload.get("importance", 0.7)),
      confidence=float(event.payload.get("confidence", 0.7)),
      created_by="memory_evolution_settlement",
    )

  def _append_memory_write_event(self, run_id: str, source_event: RuntimeEvent, item: MemoryItem) -> None:
    with self._uow_factory() as uow:
      if source_event.event_id not in item.source_event_ids:
        item.source_event_ids = [*item.source_event_ids, source_event.event_id]
        uow.memory.save(item)
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.MEMORY_WRITE,
          run_id=run_id,
          causal_id=source_event.event_id,
          payload={
            "memory_id": item.memory_id,
            "memory_type": item.memory_type,
            "scope": item.scope,
            "source": "memory_evolution_settlement",
            "candidate_id": source_event.payload.get("candidate_id"),
          },
        )
      )

  def _already_settled(self, scope: str, source_event_id: str) -> bool:
    for memory_type in ("semantic", "procedural"):
      for item in self._memory.retrieve(scope, memory_type=memory_type, limit=500):
        if source_event_id in item.source_event_ids:
          return True
    return False

  @staticmethod
  def _candidate_scope(event: RuntimeEvent, *, run_id: str, override_scope: str | None) -> str:
    if override_scope:
      return override_scope
    payload_scope = event.payload.get("scope")
    if isinstance(payload_scope, str) and payload_scope:
      return payload_scope
    return run_id

  @staticmethod
  def _target_memory_type(payload: dict[str, Any]) -> MemoryEvolutionTarget:
    requested = payload.get("memory_type")
    if requested in {"semantic", "procedural"}:
      return requested
    kind = str(payload.get("kind") or payload.get("category") or "").lower()
    note = str(payload.get("note") or "").lower()
    if kind in {"skill", "procedure", "procedural", "sop"}:
      return "procedural"
    if any(marker in note for marker in ("sop", "workflow", "procedure", "skill")):
      return "procedural"
    return "semantic"

  @staticmethod
  def _has_evidence(payload: dict[str, Any]) -> bool:
    evidence = payload.get("evidence_summary")
    if isinstance(evidence, str) and evidence.strip():
      return True
    for key in ("source_tool_call_ids", "source_event_ids", "artifact_ids"):
      values = payload.get(key)
      if isinstance(values, list) and any(isinstance(value, str) and value.strip() for value in values):
        return True
    return False


def _string_list(value: object) -> list[str]:
  if not isinstance(value, list):
    return []
  return [item for item in value if isinstance(item, str) and item]
