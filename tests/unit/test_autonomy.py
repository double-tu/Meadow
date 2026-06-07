import unittest

from agent_kernel.autonomy import (
  ExplorationExecutor,
  ExplorationPlanner,
  ExplorationService,
  ExplorationVerifier,
  PlanPatchValidator,
  SkillService,
  SkillEvolutionService,
  TraceDistiller,
  WorkflowLibrary,
)
from agent_kernel.capabilities import CapabilityRegistry
from agent_kernel.domain import EdgeSpec, ExecutionCommand, NodeSpec, PlanPatch, WorkflowSpec
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel
from agent_kernel.domain.autonomy import AttemptStatus, WorkflowTemplateStatus
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
        attempt_executor=lambda strategy: {"ok": False, "summary": "failed"},
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

      self.assertIsNone(template)
      self.assertEqual(stored_task.status, "failed")
      self.assertEqual(attempts[0].status, AttemptStatus.FAILED)
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


if __name__ == "__main__":
  unittest.main()
