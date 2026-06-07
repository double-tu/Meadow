"""Human intervention application service."""

from __future__ import annotations

from dataclasses import dataclass, replace

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.policy import HumanIntervention, InterventionApplyMode, InterventionType
from agent_kernel.domain.states import RunStatus, assert_transition
from agent_kernel.memory import MemoryFacade


@dataclass(slots=True)
class InterventionOutcome:
  intervention: HumanIntervention
  run_status: RunStatus | str | None
  memory_id: str | None = None


class HumanInterventionService:
  def __init__(self, uow_factory, memory: MemoryFacade | None = None) -> None:
    self._uow_factory = uow_factory
    self._memory = memory or MemoryFacade(uow_factory)

  def apply(
    self,
    run_id: str,
    content: str,
    intervention_type: InterventionType | str = InterventionType.CORRECTION,
    apply_mode: InterventionApplyMode | str = InterventionApplyMode.CONTINUE_NEXT_TURN,
    thread_id: str | None = None,
    task_id: str | None = None,
    priority: str = "high",
    memory_scope: str | None = None,
  ) -> InterventionOutcome:
    intervention = HumanIntervention(
      intervention_id=new_id("intervention"),
      run_id=run_id,
      type=intervention_type,
      content=content,
      apply_mode=apply_mode,
      thread_id=thread_id,
      task_id=task_id,
      priority=priority,  # type: ignore[arg-type]
    )
    memory = self._memory.write_working(
      scope=memory_scope or run_id,
      content={
        "kind": "human_intervention",
        "intervention_id": intervention.intervention_id,
        "type": intervention.type.value,
        "apply_mode": intervention.apply_mode.value,
        "priority": priority,
        "content": content,
      },
      importance=1.0,
      created_by="human",
    )
    run_status: RunStatus | None = None
    with self._uow_factory() as uow:
      state = uow.states.get(run_id)
      if state is not None and intervention.apply_mode is InterventionApplyMode.PAUSE_AND_RESUME:
        if state.status is not RunStatus.INTERRUPTED:
          assert_transition(state.status, RunStatus.INTERRUPTED)
        state = replace(state, status=RunStatus.INTERRUPTED, updated_at=utc_now())
        uow.states.save(state)
        run_status = state.status
      elif state is not None:
        run_status = state.status
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.HUMAN_INTERVENTION,
          run_id=run_id,
          task_id=task_id,
          payload={
            "intervention": intervention.to_dict(),
            "memory_id": memory.memory_id,
          },
          artifact_refs=intervention.artifact_refs,
        )
      )
    return InterventionOutcome(
      intervention=intervention,
      run_status=run_status,
      memory_id=memory.memory_id,
    )
