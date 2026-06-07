"""Minimal durable runtime engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.run import RunState
from agent_kernel.domain.stability import CircuitBreakerState, RuntimeBudget
from agent_kernel.domain.states import CircuitStatus
from agent_kernel.domain.states import NodeStepStatus, RunStatus, assert_transition
from agent_kernel.domain.step import NodeStepRecord
from agent_kernel.domain.workflow import NodeContext, WorkflowSpec
from agent_kernel.persistence.unit_of_work import UnitOfWork
from agent_kernel.runtime.budget import BudgetManager, BudgetUsage
from agent_kernel.runtime.circuit_breaker import CircuitBreaker
from agent_kernel.runtime.dead_letter import build_dead_letter
from agent_kernel.runtime.retry import RetryClassifier
from agent_kernel.workflow.executors.base import NodeExecutorRegistry
from agent_kernel.workflow.graph import WorkflowGraph
from agent_kernel.workflow.reducer import reduce_state


@dataclass(slots=True)
class RuntimeOptions:
  budget: RuntimeBudget | None = None
  budget_usage: BudgetUsage | None = None
  circuit_state: CircuitBreakerState | None = None
  worker_id: str = "runtime"


class RuntimeEngine:
  def __init__(
    self,
    uow_factory: Callable[[], UnitOfWork],
    executors: NodeExecutorRegistry,
    retry_classifier: RetryClassifier | None = None,
    budget_manager: BudgetManager | None = None,
    circuit_breaker: CircuitBreaker | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._executors = executors
    self._retry_classifier = retry_classifier or RetryClassifier()
    self._budget_manager = budget_manager or BudgetManager()
    self._circuit_breaker = circuit_breaker or CircuitBreaker()

  def create_run(
    self,
    workflow: WorkflowSpec,
    input: dict[str, Any] | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    task_id: str | None = None,
  ) -> RunState:
    run = RunState(
      run_id=run_id or new_id("run"),
      status=RunStatus.PENDING,
      thread_id=thread_id,
      workflow_id=workflow.workflow_id,
      workflow_version=workflow.version,
      task_id=task_id,
      current_node_id=workflow.start_node_id,
      variables=input or {},
      updated_at=utc_now(),
    )
    event = RuntimeEvent(
      event_type=RuntimeEventType.RUN_CREATED,
      run_id=run.run_id,
      payload={"workflow_id": workflow.workflow_id, "workflow_version": workflow.version},
    )
    with self._uow_factory() as uow:
      uow.events.append(event)
      checkpoint = uow.checkpoints.save(run.run_id, run, event.event_id)
      run = replace(run, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(run)
    return run

  async def run_until_waiting(
    self,
    workflow: WorkflowSpec,
    run_id: str,
    max_steps: int | None = None,
    options: RuntimeOptions | None = None,
  ) -> RunState:
    graph = WorkflowGraph(workflow)
    step_count = 0
    options = options or RuntimeOptions()

    while True:
      with self._uow_factory() as uow:
        state = uow.states.get(run_id)
        if state is None:
          raise KeyError(f"Run state not found: {run_id}")

      if state.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
        return state
      if state.status in {RunStatus.PAUSED, RunStatus.INTERRUPTED}:
        return state
      if max_steps is not None and step_count >= max_steps:
        return state

      budget_pause = self._pause_if_budget_exhausted(state, options)
      if budget_pause is not None:
        return budget_pause

      state = await self._execute_one_step(graph, state, options)
      step_count += 1

  def resume_from_checkpoint(self, run_id: str) -> RunState:
    with self._uow_factory() as uow:
      checkpoint = uow.checkpoints.latest_for_run(run_id)
      if checkpoint is None:
        raise KeyError(f"No checkpoint found for run: {run_id}")
      state = checkpoint.state
      if state.status in {RunStatus.PAUSED, RunStatus.INTERRUPTED, RunStatus.FAILED}:
        assert_transition(state.status, RunStatus.RUNNING)
        state = replace(state, status=RunStatus.RUNNING, updated_at=utc_now())
      uow.states.save(state)
      uow.events.append(RuntimeEvent(event_type=RuntimeEventType.RUN_RESUMED, run_id=run_id))
      return state

  def cancel_run(self, run_id: str, reason: str) -> RunState:
    with self._uow_factory() as uow:
      state = uow.states.get(run_id)
      if state is None:
        raise KeyError(f"Run state not found: {run_id}")
      if state.status is not RunStatus.CANCELLED:
        assert_transition(state.status, RunStatus.CANCELLED)
        state = replace(state, status=RunStatus.CANCELLED, updated_at=utc_now())
      event = RuntimeEvent(
        event_type=RuntimeEventType.RUN_CANCELLED,
        run_id=run_id,
        payload={"reason": reason},
      )
      uow.events.append(event)
      checkpoint = uow.checkpoints.save(run_id, state, event.event_id)
      state = replace(state, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(state)
      return state

  def pause_run(
    self,
    run_id: str,
    reason: str,
    *,
    source: str = "runtime",
    causal_id: str | None = None,
    payload: dict[str, Any] | None = None,
  ) -> RunState:
    with self._uow_factory() as uow:
      state = uow.states.get(run_id)
      if state is None:
        raise KeyError(f"Run state not found: {run_id}")
      if state.status is RunStatus.PAUSED:
        paused = state
      else:
        assert_transition(state.status, RunStatus.PAUSED)
        paused = replace(state, status=RunStatus.PAUSED, updated_at=utc_now())
      event = RuntimeEvent(
        event_type=RuntimeEventType.RUN_PAUSED,
        run_id=run_id,
        causal_id=causal_id,
        payload={
          "reason": reason,
          "source": source,
          **(payload or {}),
        },
      )
      uow.events.append(event)
      checkpoint = uow.checkpoints.save(run_id, paused, event.event_id)
      paused = replace(paused, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(paused)
      return paused

  async def _execute_one_step(
    self,
    graph: WorkflowGraph,
    state: RunState,
    options: RuntimeOptions,
  ) -> RunState:
    if state.current_node_id is None:
      return self._complete_run(state)

    if state.status is RunStatus.PENDING:
      assert_transition(state.status, RunStatus.RUNNING)
      state = replace(state, status=RunStatus.RUNNING, started_at=utc_now(), updated_at=utc_now())
      with self._uow_factory() as uow:
        uow.events.append(RuntimeEvent(event_type=RuntimeEventType.RUN_STARTED, run_id=state.run_id))
        uow.states.save(state)

    node = graph.get_node(state.current_node_id)
    circuit_pause = self._pause_if_circuit_open(state, node.node_id, options)
    if circuit_pause is not None:
      return circuit_pause

    executor = self._executors.resolve(node.kind)
    attempt = 1
    while True:
      step_id = new_id("step")
      idempotency_key = f"{state.run_id}:{node.node_id}:{attempt}"
      completed_duplicate = self._get_completed_step_by_idempotency_key(idempotency_key)
      if completed_duplicate is not None:
        return self._advance_after_duplicate_success(graph, state, node.node_id)
      ctx = NodeContext(
        run_id=state.run_id,
        step_id=step_id,
        node=node,
        state=state.variables,
        input=state.variables,
        idempotency_key=idempotency_key,
      )
      step = self._start_step(state.run_id, node.node_id, step_id, attempt, ctx.idempotency_key)
      try:
        result = await executor.execute(ctx)
      except Exception as exc:
        retry_state = self._record_step_failure_or_retry(state, step, exc)
        if retry_state.status is RunStatus.RUNNING:
          attempt += 1
          continue
        return retry_state
      break

    next_variables = reduce_state(state.variables, result.state_patch)
    next_node_id = self._next_node_id(
      graph,
      node.node_id,
      result.command.type if result.command else None,
      result.command.target if result.command else None,
      next_variables,
    )
    next_status = state.status
    if result.command is not None:
      if result.command.type == "finish":
        next_status = RunStatus.COMPLETED
        next_node_id = None
      elif result.command.type == "fail":
        next_status = RunStatus.FAILED
        next_node_id = None
      elif result.command.type == "interrupt":
        next_status = RunStatus.INTERRUPTED
        next_node_id = node.node_id
      elif result.command.type == "request_approval":
        next_status = RunStatus.INTERRUPTED
        next_node_id = node.node_id
      elif result.command.type == "goto" and result.command.target is None:
        next_status = RunStatus.FAILED

    next_state = replace(
      state,
      status=next_status,
      current_node_id=next_node_id,
      variables=next_variables,
      artifact_refs=[*state.artifact_refs, *result.artifact_refs],
      updated_at=utc_now(),
    )
    event_type = RuntimeEventType.STEP_COMPLETED
    if next_status is RunStatus.FAILED:
      event_type = RuntimeEventType.STEP_FAILED
    completed_event = RuntimeEvent(
      event_type=event_type,
      run_id=state.run_id,
      node_id=node.node_id,
      step_id=step_id,
      payload={"command": result.command.to_dict() if result.command else {"type": "continue"}},
      artifact_refs=result.artifact_refs,
    )

    with self._uow_factory() as uow:
      if next_status is RunStatus.FAILED:
        step = replace(step, status=NodeStepStatus.FAILED, error="step command failed", updated_at=utc_now())
      elif next_status is RunStatus.CANCELLED:
        step = replace(step, status=NodeStepStatus.CANCELLED, updated_at=utc_now())
      elif next_status is RunStatus.INTERRUPTED:
        step = replace(step, status=NodeStepStatus.INTERRUPTED, updated_at=utc_now())
      else:
        step = replace(step, status=NodeStepStatus.SUCCEEDED, updated_at=utc_now())
      uow.steps.save(step)
      for event in result.events:
        uow.events.append(event)
      uow.events.append(completed_event)
      if next_status is RunStatus.COMPLETED:
        uow.events.append(RuntimeEvent(event_type=RuntimeEventType.RUN_COMPLETED, run_id=state.run_id))
      elif next_status is RunStatus.FAILED:
        uow.events.append(RuntimeEvent(event_type=RuntimeEventType.RUN_FAILED, run_id=state.run_id))
      checkpoint = uow.checkpoints.save(state.run_id, next_state, completed_event.event_id)
      next_state = replace(next_state, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(next_state)

    return next_state

  def _get_completed_step_by_idempotency_key(self, idempotency_key: str) -> NodeStepRecord | None:
    with self._uow_factory() as uow:
      step = uow.steps.get_by_idempotency_key(idempotency_key)
      if step is not None and step.status is NodeStepStatus.SUCCEEDED:
        return step
    return None

  def _advance_after_duplicate_success(
    self,
    graph: WorkflowGraph,
    state: RunState,
    node_id: str,
  ) -> RunState:
    next_node_id = graph.next_node_id(node_id, state.variables)
    next_state = replace(state, current_node_id=next_node_id, updated_at=utc_now())
    if next_node_id is None:
      next_state = replace(next_state, status=RunStatus.COMPLETED)
    with self._uow_factory() as uow:
      event = RuntimeEvent(
        event_type=RuntimeEventType.STEP_COMPLETED,
        run_id=state.run_id,
        node_id=node_id,
        payload={"deduplicated": True},
      )
      uow.events.append(event)
      if next_state.status is RunStatus.COMPLETED:
        uow.events.append(RuntimeEvent(event_type=RuntimeEventType.RUN_COMPLETED, run_id=state.run_id))
      checkpoint = uow.checkpoints.save(state.run_id, next_state, event.event_id)
      next_state = replace(next_state, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(next_state)
    return next_state

  def _start_step(
    self,
    run_id: str,
    node_id: str,
    step_id: str,
    attempt: int,
    idempotency_key: str,
  ) -> NodeStepRecord:
    step = NodeStepRecord(
      step_id=step_id,
      run_id=run_id,
      node_id=node_id,
      status=NodeStepStatus.SCHEDULED,
      attempt=attempt,
      idempotency_key=idempotency_key,
    )
    with self._uow_factory() as uow:
      step = replace(step, status=NodeStepStatus.LEASED, updated_at=utc_now())
      uow.steps.save(step)
      step = replace(step, status=NodeStepStatus.RUNNING, updated_at=utc_now())
      uow.steps.save(step)
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.STEP_STARTED,
          run_id=run_id,
          node_id=node_id,
          step_id=step_id,
          payload={"attempt": attempt, "idempotency_key": idempotency_key},
        )
      )
    return step

  def _complete_run(self, state: RunState) -> RunState:
    assert_transition(state.status, RunStatus.COMPLETED)
    completed = replace(state, status=RunStatus.COMPLETED, updated_at=utc_now())
    with self._uow_factory() as uow:
      uow.events.append(RuntimeEvent(event_type=RuntimeEventType.RUN_COMPLETED, run_id=state.run_id))
      checkpoint = uow.checkpoints.save(state.run_id, completed)
      completed = replace(completed, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(completed)
    return completed

  def _record_step_failure_or_retry(
    self,
    state: RunState,
    step: NodeStepRecord,
    exc: BaseException,
  ) -> RunState:
    decision = self._retry_classifier.classify(exc, step.attempt)
    reason = decision.reason or str(exc)
    with self._uow_factory() as uow:
      step = replace(
        step,
        status=NodeStepStatus.RETRY_WAIT if decision.retryable else NodeStepStatus.FAILED,
        error=reason,
        updated_at=utc_now(),
      )
      uow.steps.save(step)
      event = RuntimeEvent(
        event_type=RuntimeEventType.STEP_FAILED,
        run_id=state.run_id,
        node_id=step.node_id,
        step_id=step.step_id,
        payload={
          "reason": reason,
          "failure_type": decision.failure_type,
          "retryable": decision.retryable,
        },
      )
      uow.events.append(event)
      if decision.retryable:
        retry_event = RuntimeEvent(
          event_type=RuntimeEventType.STEP_STARTED,
          run_id=state.run_id,
          node_id=step.node_id,
          step_id=step.step_id,
          payload={
            "retry_scheduled": True,
            "next_attempt": decision.next_attempt,
            "delay_seconds": decision.delay_seconds,
          },
        )
        uow.events.append(retry_event)
        uow.steps.save(replace(step, status=NodeStepStatus.SCHEDULED, updated_at=utc_now()))
        uow.states.save(state)
        return state

      failed = replace(state, status=RunStatus.FAILED, updated_at=utc_now())
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.RUN_FAILED,
          run_id=state.run_id,
          payload={"reason": reason, "failure_type": decision.failure_type},
        )
      )
      if not decision.retryable:
        uow.dead_letters.add(
          build_dead_letter(
            target_type="node_step",
            target_id=step.step_id,
            reason=reason,
            failure_type=decision.failure_type,
            retryable=False,
            event_refs=[event.event_id],
          )
        )
      checkpoint = uow.checkpoints.save(state.run_id, failed, event.event_id)
      failed = replace(failed, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(failed)
    return failed

  def _pause_if_budget_exhausted(
    self,
    state: RunState,
    options: RuntimeOptions,
  ) -> RunState | None:
    if options.budget is None:
      return None
    usage = options.budget_usage or BudgetUsage()
    reason = self._budget_manager.exhausted_reason(options.budget, usage)
    if reason is None:
      return None
    if options.budget.exhausted_action == "cancel":
      return self.cancel_run(state.run_id, reason)
    paused = replace(state, status=RunStatus.PAUSED, updated_at=utc_now())
    with self._uow_factory() as uow:
      event = RuntimeEvent(
        event_type=RuntimeEventType.RUN_PAUSED,
        run_id=state.run_id,
        payload={"reason": reason, "source": "budget"},
      )
      uow.events.append(event)
      checkpoint = uow.checkpoints.save(state.run_id, paused, event.event_id)
      paused = replace(paused, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(paused)
    return paused

  def _pause_if_circuit_open(
    self,
    state: RunState,
    node_id: str,
    options: RuntimeOptions,
  ) -> RunState | None:
    if options.circuit_state is None:
      return None
    circuit_state = self._circuit_breaker.maybe_probe(options.circuit_state)
    if circuit_state.status is not CircuitStatus.OPEN:
      return None
    paused = replace(state, status=RunStatus.PAUSED, updated_at=utc_now())
    with self._uow_factory() as uow:
      event = RuntimeEvent(
        event_type=RuntimeEventType.RUN_PAUSED,
        run_id=state.run_id,
        node_id=node_id,
        payload={"reason": "circuit_open", "target_ref": circuit_state.target_ref},
      )
      uow.events.append(event)
      checkpoint = uow.checkpoints.save(state.run_id, paused, event.event_id)
      paused = replace(paused, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(paused)
    return paused

  def _fail_run(self, state: RunState, node_id: str, step_id: str, reason: str) -> RunState:
    failed = replace(state, status=RunStatus.FAILED, updated_at=utc_now())
    with self._uow_factory() as uow:
      event = RuntimeEvent(
        event_type=RuntimeEventType.STEP_FAILED,
        run_id=state.run_id,
        node_id=node_id,
        step_id=step_id,
        payload={"reason": reason},
      )
      uow.events.append(event)
      uow.events.append(
        RuntimeEvent(event_type=RuntimeEventType.RUN_FAILED, run_id=state.run_id, payload={"reason": reason})
      )
      checkpoint = uow.checkpoints.save(state.run_id, failed, event.event_id)
      failed = replace(failed, checkpoint_id=checkpoint.checkpoint_id)
      uow.states.save(failed)
    return failed

  @staticmethod
  def _next_node_id(
    graph: WorkflowGraph,
    node_id: str,
    command_type: str | None,
    command_target: str | None,
    variables: dict[str, Any],
  ) -> str | None:
    if command_type == "goto":
      return command_target
    if command_type in {"finish", "fail"}:
      return None
    return graph.next_node_id(node_id, variables)
