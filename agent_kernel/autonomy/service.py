"""Autonomous exploration service."""

from dataclasses import replace

from agent_kernel.autonomy.explorer import ExplorationExecutor
from agent_kernel.autonomy.planner import ExplorationPlanner
from agent_kernel.autonomy.trace_distiller import TraceDistiller
from agent_kernel.autonomy.workflow_library import WorkflowLibrary
from agent_kernel.domain.autonomy import (
  AcceptanceCriteria,
  AttemptStatus,
  ExplorationStatus,
  ExplorationTask,
  WorkflowTemplate,
)
from agent_kernel.domain.base import new_id, utc_now


class ExplorationService:
  def __init__(
    self,
    uow_factory,
    planner: ExplorationPlanner,
    executor: ExplorationExecutor,
    distiller: TraceDistiller,
    workflow_library: WorkflowLibrary,
  ) -> None:
    self._uow_factory = uow_factory
    self._planner = planner
    self._executor = executor
    self._distiller = distiller
    self._workflow_library = workflow_library

  def create_task(
    self,
    objective_id: str,
    problem_statement: str,
    acceptance_criteria: list[str] | None = None,
    max_attempts: int = 3,
  ) -> ExplorationTask:
    task = ExplorationTask(
      exploration_id=new_id("exploration"),
      objective_id=objective_id,
      status=ExplorationStatus.CREATED,
      problem_statement=problem_statement,
      acceptance_criteria=[
        AcceptanceCriteria(criteria_id=new_id("criteria"), description=item)
        for item in (acceptance_criteria or [])
      ],
      max_attempts=max_attempts,
    )
    with self._uow_factory() as uow:
      uow.autonomy.save_exploration(task)
    return task

  def run(self, task: ExplorationTask) -> WorkflowTemplate | None:
    planning = replace(task, status=ExplorationStatus.PLANNING, updated_at=utc_now())
    strategies = self._planner.generate(planning)
    with self._uow_factory() as uow:
      uow.autonomy.save_exploration(planning)
      for strategy in strategies:
        uow.autonomy.save_strategy(strategy)
    for strategy in sorted(strategies, key=lambda item: item.priority, reverse=True)[: task.max_attempts]:
      attempt = self._executor.execute(strategy)
      if attempt.status is AttemptStatus.SUCCEEDED:
        trace = self._distiller.distill(attempt)
        with self._uow_factory() as uow:
          uow.autonomy.save_golden_trace(trace)
          uow.autonomy.save_exploration(
            replace(task, status=ExplorationStatus.PUBLISHED, updated_at=utc_now())
          )
        return self._workflow_library.publish_draft(
          trace=trace,
          name=f"Draft workflow for {task.objective_id}",
          workflow_spec_ref=f"workflow://draft/{trace.trace_id}",
          applicability=task.problem_statement,
        )
    with self._uow_factory() as uow:
      uow.autonomy.save_exploration(
        replace(task, status=ExplorationStatus.FAILED, updated_at=utc_now())
      )
    return None

