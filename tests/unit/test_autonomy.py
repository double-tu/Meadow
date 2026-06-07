import unittest

from agent_kernel.domain.errors import DomainValidationError
from agent_kernel.autonomy import (
  CompositeExplorationPlanner,
  DeterministicExplorationReflector,
  ExplorationExecutor,
  ExplorationPlanner,
  ExplorationService,
  ExplorationVerifier,
  PlanPatchValidator,
  SkillService,
  SkillEvolutionService,
  TraceDistiller,
  WorkflowPatchApplier,
  WorkflowLibrary,
)
from agent_kernel.capabilities import CapabilityRegistry
from agent_kernel.domain import EdgeSpec, ExecutionCommand, NodeSpec, PlanPatch, RuntimeEventType, WorkflowSpec
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel
from agent_kernel.domain.autonomy import (
  AttemptStatus,
  CandidateStrategy,
  ExplorationAttempt,
  ExplorationStatus,
  ExplorationTask,
  ReflectionStatus,
  WorkflowTemplateStatus,
)
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class AutonomyTests(unittest.TestCase):
  def test_exploration_publishes_draft_workflow_template_from_successful_attempt(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      executor = ExplorationExecutor(
        uow_factory,
        attempt_executor=lambda strategy: {
          "ok": True,
          "summary": "success",
          "event_refs": ["evt_1", "evt_2"],
        },
        verifier=ExplorationVerifier(),
      )
      service = ExplorationService(
        uow_factory,
        planner=ExplorationPlanner(),
        executor=executor,
        distiller=TraceDistiller(),
        workflow_library=WorkflowLibrary(uow_factory),
      )

      task = service.create_task(
        objective_id="obj_1",
        problem_statement="solve unknown task",
        acceptance_criteria=["works"],
      )
      template = service.run(task)

      self.assertIsNotNone(template)
      self.assertEqual(template.status, WorkflowTemplateStatus.DRAFT)
      self.assertEqual(template.applicability, "solve unknown task")

      with UnitOfWork(conn) as uow:
        stored_task = uow.autonomy.get_exploration(task.exploration_id)
        strategies = uow.autonomy.list_strategies(task.exploration_id)
        attempts = uow.autonomy.list_attempts(task.exploration_id)
        templates = uow.autonomy.list_workflow_templates()

      self.assertEqual(stored_task.status, "published")
      self.assertEqual(len(strategies), 1)
      self.assertEqual(attempts[0].status, AttemptStatus.SUCCEEDED)
      self.assertEqual(templates[0].template_id, template.template_id)
    finally:
      conn.close()

  def test_skill_evolution_records_compiled_workflow(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      executor = ExplorationExecutor(
        uow_factory,
        attempt_executor=lambda strategy: {"ok": True, "summary": "success", "event_refs": ["evt_1"]},
        verifier=ExplorationVerifier(),
      )
      strategy = ExplorationPlanner().generate(
        service_task := ExplorationService(
          uow_factory,
          planner=ExplorationPlanner(),
          executor=executor,
          distiller=TraceDistiller(),
          workflow_library=WorkflowLibrary(uow_factory),
        ).create_task("obj_1", "compile this")
      )[0]
      attempt = executor.execute(strategy)
      trace = TraceDistiller().distill(attempt)
      template = WorkflowLibrary(uow_factory).publish_draft(
        trace,
        name="compiled",
        workflow_spec_ref="workflow://compiled",
        applicability="compile this",
      )

      record = SkillEvolutionService(uow_factory).record_compiled_workflow(
        trace,
        template,
        rationale="successful trace",
      )

      self.assertEqual(record.decision, "compile_workflow")
      self.assertEqual(record.workflow_template_id, template.template_id)
      self.assertEqual(service_task.problem_statement, "compile this")
    finally:
      conn.close()

  def test_exploration_returns_none_when_attempt_fails(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      executor = ExplorationExecutor(
        uow_factory,
        attempt_executor=lambda strategy: {"ok": False, "summary": "timeout waiting for permission"},
        verifier=ExplorationVerifier(),
      )
      service = ExplorationService(
        uow_factory,
        planner=ExplorationPlanner(),
        executor=executor,
        distiller=TraceDistiller(),
        workflow_library=WorkflowLibrary(uow_factory),
      )

      task = service.create_task("obj_1", "hard task", max_attempts=1)
      template = service.run(task)

      with UnitOfWork(conn) as uow:
        stored_task = uow.autonomy.get_exploration(task.exploration_id)
        attempts = uow.autonomy.list_attempts(task.exploration_id)
        reflections = uow.autonomy.list_reflections(task.exploration_id)

      self.assertIsNone(template)
      self.assertEqual(stored_task.status, "failed")
      self.assertEqual(attempts[0].status, AttemptStatus.FAILED)
      self.assertEqual(reflections[0].status, ReflectionStatus.PROPOSED)
      self.assertEqual(reflections[0].root_causes, ["timeout", "permission"])
      self.assertIn("required capability grants", " ".join(reflections[0].next_strategy_hints))
    finally:
      conn.close()

  def test_deterministic_reflector_classifies_failed_attempt(self) -> None:
    exploration = ExplorationTask(
      exploration_id="exploration_1",
      objective_id="obj_1",
      status=ExplorationStatus.FAILED,
      problem_statement="replace manual workflow",
    )
    strategy = CandidateStrategy(
      strategy_id="strategy_1",
      exploration_id=exploration.exploration_id,
      hypothesis="Use direct browser automation.",
    )
    attempt = ExplorationAttempt(
      attempt_id="attempt_1",
      exploration_id=exploration.exploration_id,
      strategy_id=strategy.strategy_id,
      run_id="run_1",
      status=AttemptStatus.FAILED,
      failure_reason="missing browser session",
    )

    reflection = DeterministicExplorationReflector().reflect(exploration, strategy, attempt)

    self.assertEqual(reflection.root_causes, ["missing_dependency"])
    self.assertIn("discovery step", " ".join(reflection.next_strategy_hints))
    self.assertIn("assuming unavailable tools", " ".join(reflection.avoid_patterns))

  def test_composite_planner_runs_multiple_strategies_until_success(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      calls: list[str] = []

      def execute(strategy: CandidateStrategy) -> dict[str, object]:
        calls.append(strategy.hypothesis)
        if "Discover constraints" in strategy.hypothesis:
          return {"ok": True, "summary": "discovered and solved", "event_refs": ["evt_success"]}
        return {"ok": False, "summary": "timeout on direct path"}

      executor = ExplorationExecutor(
        uow_factory,
        attempt_executor=execute,
        verifier=ExplorationVerifier(),
      )
      service = ExplorationService(
        uow_factory,
        planner=CompositeExplorationPlanner(),
        executor=executor,
        distiller=TraceDistiller(),
        workflow_library=WorkflowLibrary(uow_factory),
      )

      task = service.create_task("obj_multi", "replace manual browser workflow", max_attempts=3)
      template = service.run(task)

      with UnitOfWork(conn) as uow:
        strategies = uow.autonomy.list_strategies(task.exploration_id)
        attempts = uow.autonomy.list_attempts(task.exploration_id)
        reflections = uow.autonomy.list_reflections(task.exploration_id)

      self.assertIsNotNone(template)
      self.assertEqual(len(strategies), 3)
      self.assertEqual(len(attempts), 2)
      self.assertEqual(attempts[0].status, AttemptStatus.FAILED)
      self.assertEqual(attempts[1].status, AttemptStatus.SUCCEEDED)
      self.assertEqual(len(reflections), 1)
      self.assertIn("Attempt direct solution", calls[0])
      self.assertIn("Discover constraints", calls[1])
    finally:
      conn.close()

  def test_skill_service_creates_activates_and_selects_skill(self) -> None:
    conn = connect_sqlite()
    try:
      service = SkillService(unit_of_work_factory(conn))
      skill = service.create_interpreted_skill(
        name="api-review",
        description="Review API design",
        when_to_use="api design",
        instructions="Check request and response contracts.",
      )
      active = service.activate(skill.skill_id)
      selected = service.select_for_prompt("api design")

      self.assertEqual(active.status, "active")
      self.assertEqual(selected[0].skill_id, skill.skill_id)
    finally:
      conn.close()

  def test_workflow_library_registers_and_resolves_workflow_specs(self) -> None:
    conn = connect_sqlite()
    try:
      library = WorkflowLibrary(unit_of_work_factory(conn))
      workflow = WorkflowSpec(
        workflow_id="wf_registered",
        version="0.1.0",
        name="registered",
        input_schema={},
        output_schema={},
        nodes=[NodeSpec(node_id="start", kind="noop")],
        edges=[],
        start_node_id="start",
      )

      registered = library.register_workflow(workflow)
      resolved = library.get_workflow(WorkflowLibrary.workflow_ref(workflow))

      self.assertEqual(registered.workflow_id, "wf_registered")
      self.assertIsNotNone(resolved)
      self.assertEqual(resolved.start_node_id, "start")
      self.assertEqual(library.list_workflows("wf_registered")[0].version, "0.1.0")
    finally:
      conn.close()

  def test_compiled_workflow_skill_registers_workflow_for_resolution(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      library = WorkflowLibrary(uow_factory)
      service = SkillService(uow_factory)
      workflow = WorkflowSpec(
        workflow_id="wf_skill",
        version="1.0.0",
        name="skill workflow",
        input_schema={},
        output_schema={},
        nodes=[NodeSpec(node_id="start", kind="noop")],
        edges=[],
        start_node_id="start",
      )

      skill = service.create_compiled_workflow_skill(
        workflow,
        name="compiled-skill",
        description="Use compiled workflow",
        when_to_use="compiled",
        instructions="Run the compiled workflow.",
        workflow_library=library,
      )
      resolved = library.get_workflow(skill.compiled_workflow_ref)

      self.assertEqual(skill.compiled_workflow_ref, "workflow://wf_skill/1.0.0")
      self.assertIsNotNone(resolved)
      self.assertEqual(resolved.workflow_id, "wf_skill")
    finally:
      conn.close()

  def test_workflow_library_rejects_invalid_edge_condition(self) -> None:
    conn = connect_sqlite()
    try:
      library = WorkflowLibrary(unit_of_work_factory(conn))
      workflow = WorkflowSpec(
        workflow_id="wf_invalid_condition",
        version="0.1.0",
        name="invalid condition",
        input_schema={},
        output_schema={},
        nodes=[NodeSpec(node_id="start", kind="noop"), NodeSpec(node_id="next", kind="noop")],
        edges=[EdgeSpec(from_node="start", to_node="next", condition="unsupported()")],
        start_node_id="start",
      )

      with self.assertRaises(DomainValidationError):
        library.register_workflow(workflow)
    finally:
      conn.close()

  def test_plan_patch_validator_checks_targets_and_capabilities(self) -> None:
    workflow = WorkflowSpec(
      workflow_id="wf_patch",
      version="0.1.0",
      name="patch",
      input_schema={},
      output_schema={},
      nodes=[NodeSpec(node_id="start", kind="noop"), NodeSpec(node_id="next", kind="noop")],
      edges=[EdgeSpec(from_node="start", to_node="next")],
      start_node_id="start",
    )
    capabilities = CapabilityRegistry()
    capabilities.register(
      CapabilitySpec(
        capability_id="tool.safe",
        name="safe",
        kind="tool",
        input_schema={},
        output_schema={},
        side_effect_level=SideEffectLevel.NONE,
      )
    )
    ok_patch = PlanPatch(
      patch_id="patch_ok",
      run_id="run_1",
      proposed_by_agent_id="agent_1",
      reason="continue",
      commands=[ExecutionCommand(type="goto", target="next")],
      required_capabilities=["tool.safe"],
    )
    bad_patch = PlanPatch(
      patch_id="patch_bad",
      run_id="run_1",
      proposed_by_agent_id="agent_1",
      reason="bad",
      commands=[ExecutionCommand(type="goto", target="missing")],
      required_capabilities=["tool.missing"],
    )

    validator = PlanPatchValidator(capabilities)

    self.assertTrue(validator.validate(ok_patch, workflow).ok)
    result = validator.validate(bad_patch, workflow)
    self.assertFalse(result.ok)
    self.assertEqual(len(result.errors), 2)

  def test_workflow_patch_applier_adds_node_edge_and_start_without_mutating_original(self) -> None:
    workflow = WorkflowSpec(
      workflow_id="wf_patch_apply",
      version="1.2.3",
      name="patch apply",
      input_schema={},
      output_schema={},
      nodes=[NodeSpec(node_id="start", kind="noop")],
      edges=[],
      start_node_id="start",
    )
    patch = PlanPatch(
      patch_id="patch_apply",
      run_id="run_patch",
      proposed_by_agent_id="agent_1",
      reason="insert verification step",
      commands=[
        ExecutionCommand(
          type="emit_event",
          payload={
            "operation": "add_node",
            "node_id": "verify",
            "kind": "tool",
            "capability_ref": "tool.verify",
            "timeout_seconds": 5,
          },
        ),
        ExecutionCommand(
          type="emit_event",
          payload={"operation": "add_edge", "from_node": "start", "to_node": "verify"},
        ),
        ExecutionCommand(
          type="emit_event",
          payload={"operation": "set_start", "node_id": "verify"},
        ),
      ],
    )

    result = WorkflowPatchApplier().apply(patch, workflow)

    self.assertEqual(workflow.version, "1.2.3")
    self.assertEqual(len(workflow.nodes), 1)
    self.assertEqual(result.workflow.version, "1.2.4")
    self.assertEqual(result.workflow.start_node_id, "verify")
    self.assertEqual(result.workflow.nodes[-1].capability_ref, "tool.verify")
    self.assertEqual(result.workflow.edges[0].to_node, "verify")
    self.assertEqual(result.event.event_type, RuntimeEventType.PLAN_PATCH_APPLIED)
    self.assertEqual(result.event.payload["patch_id"], "patch_apply")

  def test_workflow_patch_applier_rejects_invalid_edge(self) -> None:
    workflow = WorkflowSpec(
      workflow_id="wf_patch_bad",
      version="0.1.0",
      name="patch bad",
      input_schema={},
      output_schema={},
      nodes=[NodeSpec(node_id="start", kind="noop")],
      edges=[],
      start_node_id="start",
    )
    patch = PlanPatch(
      patch_id="patch_bad_edge",
      run_id="run_patch",
      proposed_by_agent_id="agent_1",
      reason="bad edge",
      commands=[
        ExecutionCommand(
          type="emit_event",
          payload={"operation": "add_edge", "from_node": "start", "to_node": "missing"},
        )
      ],
    )

    with self.assertRaisesRegex(ValueError, "Plan patch is invalid"):
      WorkflowPatchApplier().apply(patch, workflow)


if __name__ == "__main__":
  unittest.main()
