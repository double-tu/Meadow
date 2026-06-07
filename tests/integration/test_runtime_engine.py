import unittest

from agent_kernel.domain import (
  CircuitBreakerState,
  CircuitStatus,
  EdgeSpec,
  ExecutionCommand,
  NodeContext,
  NodeResult,
  NodeSpec,
  RunStatus,
  RuntimeBudget,
  RuntimeEventType,
  WorkflowSpec,
)
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import RuntimeEngine, unit_of_work_factory
from agent_kernel.runtime.budget import BudgetUsage
from agent_kernel.runtime.engine import RuntimeOptions
from agent_kernel.runtime.retry import RetryClassifier
from agent_kernel.workflow import FunctionNodeExecutor, NodeExecutorRegistry


def build_three_node_workflow() -> WorkflowSpec:
  return WorkflowSpec(
    workflow_id="wf_three_nodes",
    version="0.1.0",
    name="three nodes",
    input_schema={},
    output_schema={},
    nodes=[
      NodeSpec(node_id="start", kind="test"),
      NodeSpec(node_id="middle", kind="test"),
      NodeSpec(node_id="finish", kind="test"),
    ],
    edges=[
      EdgeSpec(from_node="start", to_node="middle"),
      EdgeSpec(from_node="middle", to_node="finish"),
    ],
    start_node_id="start",
  )


def build_engine(conn) -> RuntimeEngine:
  registry = NodeExecutorRegistry()

  def execute(ctx: NodeContext) -> NodeResult:
    if ctx.node.node_id == "finish":
      return NodeResult(
        state_patch={"finish": True},
        command=ExecutionCommand(type="finish"),
      )
    return NodeResult(state_patch={ctx.node.node_id: True})

  registry.register("test", lambda: FunctionNodeExecutor(execute))
  return RuntimeEngine(unit_of_work_factory(conn), registry)


class RuntimeEngineIntegrationTests(unittest.IsolatedAsyncioTestCase):
  async def test_runs_three_node_workflow_to_completion(self) -> None:
    conn = connect_sqlite()
    try:
      workflow = build_three_node_workflow()
      engine = build_engine(conn)

      created = engine.create_run(workflow, input={"request": "go"}, run_id="run_1")
      completed = await engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(completed.status, RunStatus.COMPLETED)
      self.assertEqual(
        completed.variables,
        {"request": "go", "start": True, "middle": True, "finish": True},
      )

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_1")
        latest_checkpoint = uow.checkpoints.latest_for_run("run_1")
    finally:
      conn.close()

    self.assertEqual(events[0].event_type, RuntimeEventType.RUN_CREATED)
    self.assertIn(RuntimeEventType.RUN_COMPLETED, [event.event_type for event in events])
    self.assertIsNotNone(latest_checkpoint)
    self.assertEqual(latest_checkpoint.state.status, RunStatus.COMPLETED)

  async def test_continues_from_persisted_checkpoint_after_engine_recreation(self) -> None:
    conn = connect_sqlite()
    try:
      workflow = build_three_node_workflow()
      first_engine = build_engine(conn)

      created = first_engine.create_run(workflow, input={}, run_id="run_resume")
      after_one_step = await first_engine.run_until_waiting(workflow, created.run_id, max_steps=1)

      self.assertEqual(after_one_step.status, RunStatus.RUNNING)
      self.assertEqual(after_one_step.current_node_id, "middle")

      second_engine = build_engine(conn)
      completed = await second_engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(completed.status, RunStatus.COMPLETED)
      self.assertEqual(completed.variables["start"], True)
      self.assertEqual(completed.variables["middle"], True)
      self.assertEqual(completed.variables["finish"], True)
    finally:
      conn.close()

  async def test_cancel_running_workflow_persists_cancel_event(self) -> None:
    conn = connect_sqlite()
    try:
      workflow = build_three_node_workflow()
      engine = build_engine(conn)

      created = engine.create_run(workflow, input={}, run_id="run_cancel")
      running = await engine.run_until_waiting(workflow, created.run_id, max_steps=1)
      cancelled = engine.cancel_run(running.run_id, reason="user requested stop")

      self.assertEqual(cancelled.status, RunStatus.CANCELLED)

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_cancel")
        latest_checkpoint = uow.checkpoints.latest_for_run("run_cancel")

      self.assertIn(RuntimeEventType.RUN_CANCELLED, [event.event_type for event in events])
      self.assertIsNotNone(latest_checkpoint)
      self.assertEqual(latest_checkpoint.state.status, RunStatus.CANCELLED)
    finally:
      conn.close()

  async def test_deterministic_step_failure_goes_to_dead_letter(self) -> None:
    conn = connect_sqlite()
    try:
      workflow = build_three_node_workflow()
      registry = NodeExecutorRegistry()

      def fail(ctx: NodeContext) -> NodeResult:
        if ctx.node.node_id == "start":
          raise ValueError("invalid input")
        return NodeResult()

      registry.register("test", lambda: FunctionNodeExecutor(fail))
      engine = RuntimeEngine(unit_of_work_factory(conn), registry)

      created = engine.create_run(workflow, input={}, run_id="run_fail")
      failed = await engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(failed.status, RunStatus.FAILED)

      with UnitOfWork(conn) as uow:
        steps = uow.steps.list_by_run("run_fail")
        dead_letters = uow.dead_letters.list_all()
        events = uow.events.list_by_run("run_fail")

      self.assertEqual(steps[0].status, "failed")
      self.assertEqual(dead_letters[0].target_type, "node_step")
      self.assertEqual(dead_letters[0].failure_type, "deterministic")
      self.assertIn(RuntimeEventType.RUN_FAILED, [event.event_type for event in events])
    finally:
      conn.close()

  async def test_transient_step_failure_retries_and_then_succeeds(self) -> None:
    conn = connect_sqlite()
    try:
      workflow = build_three_node_workflow()
      registry = NodeExecutorRegistry()
      calls = {"start": 0}

      def flaky(ctx: NodeContext) -> NodeResult:
        if ctx.node.node_id == "start":
          calls["start"] += 1
          if calls["start"] == 1:
            raise TimeoutError("timeout calling tool")
        if ctx.node.node_id == "finish":
          return NodeResult(command=ExecutionCommand(type="finish"))
        return NodeResult(state_patch={ctx.node.node_id: True})

      registry.register("test", lambda: FunctionNodeExecutor(flaky))
      engine = RuntimeEngine(
        unit_of_work_factory(conn),
        registry,
        retry_classifier=RetryClassifier(max_attempts=2, base_delay_seconds=0),
      )

      created = engine.create_run(workflow, input={}, run_id="run_retry")
      completed = await engine.run_until_waiting(workflow, created.run_id)

      self.assertEqual(completed.status, RunStatus.COMPLETED)
      self.assertEqual(calls["start"], 2)

      with UnitOfWork(conn) as uow:
        steps = uow.steps.list_by_run("run_retry")
        dead_letters = uow.dead_letters.list_all()

      self.assertEqual([step.attempt for step in steps if step.node_id == "start"], [1, 2])
      self.assertEqual(dead_letters, [])
    finally:
      conn.close()

  async def test_budget_exhaustion_pauses_run_before_next_step(self) -> None:
    conn = connect_sqlite()
    try:
      workflow = build_three_node_workflow()
      engine = build_engine(conn)
      budget = RuntimeBudget(
        budget_id="budget_1",
        scope="run",
        scope_id="run_budget",
        max_tool_calls=0,
        exhausted_action="pause",
      )

      created = engine.create_run(workflow, input={}, run_id="run_budget")
      paused = await engine.run_until_waiting(
        workflow,
        created.run_id,
        options=RuntimeOptions(budget=budget, budget_usage=BudgetUsage(tool_calls=0)),
      )

      self.assertEqual(paused.status, RunStatus.PAUSED)

      with UnitOfWork(conn) as uow:
        events = uow.events.list_by_run("run_budget")

      self.assertIn(RuntimeEventType.RUN_PAUSED, [event.event_type for event in events])
    finally:
      conn.close()

  async def test_open_circuit_pauses_run_before_node_execution(self) -> None:
    conn = connect_sqlite()
    try:
      workflow = build_three_node_workflow()
      engine = build_engine(conn)
      circuit = CircuitBreakerState(
        circuit_id="circuit_1",
        target_ref="test",
        status=CircuitStatus.OPEN,
      )

      created = engine.create_run(workflow, input={}, run_id="run_circuit")
      paused = await engine.run_until_waiting(
        workflow,
        created.run_id,
        options=RuntimeOptions(circuit_state=circuit),
      )

      self.assertEqual(paused.status, RunStatus.PAUSED)
      self.assertEqual(paused.current_node_id, "start")

      with UnitOfWork(conn) as uow:
        steps = uow.steps.list_by_run("run_circuit")

      self.assertEqual(steps, [])
    finally:
      conn.close()

  async def test_completed_idempotency_key_prevents_duplicate_node_execution(self) -> None:
    conn = connect_sqlite()
    try:
      workflow = build_three_node_workflow()
      registry = NodeExecutorRegistry()
      calls = {"count": 0}

      def counted(ctx: NodeContext) -> NodeResult:
        calls["count"] += 1
        return NodeResult(state_patch={ctx.node.node_id: True})

      registry.register("test", lambda: FunctionNodeExecutor(counted))
      engine = RuntimeEngine(unit_of_work_factory(conn), registry)

      created = engine.create_run(workflow, input={}, run_id="run_idempotent")
      after_first = await engine.run_until_waiting(workflow, created.run_id, max_steps=1)

      with UnitOfWork(conn) as uow:
        duplicated_state = after_first
        duplicated_state = type(after_first).from_dict(
          {**after_first.to_dict(), "current_node_id": "start"}
        )
        uow.states.save(duplicated_state)

      after_duplicate = await engine.run_until_waiting(workflow, created.run_id, max_steps=1)

      self.assertEqual(calls["count"], 1)
      self.assertEqual(after_duplicate.current_node_id, "middle")
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
