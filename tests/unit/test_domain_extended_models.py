import unittest

from agent_kernel.domain import (
  AcceptanceCriteria,
  CandidateStrategy,
  CircuitBreakerState,
  CircuitStatus,
  ExplorationStatus,
  ExplorationTask,
  ExtensionContribution,
  ExtensionManifest,
  PlanPatch,
  PlanPatchStatus,
  SkillCard,
  SkillExecutionMode,
  SkillStatus,
)
from agent_kernel.domain.capability import SideEffectLevel
from agent_kernel.domain.workflow import ExecutionCommand


class ExtendedDomainModelTests(unittest.TestCase):
  def test_skill_card_round_trips_execution_mode(self) -> None:
    card = SkillCard(
      skill_id="skill_1",
      name="Explore Unknown Task",
      description="Guide an agent through bounded exploration.",
      when_to_use="No fixed workflow is available.",
      instructions="Generate strategies, attempt, verify, reflect.",
      status=SkillStatus.ACTIVE,
      execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    )

    restored = SkillCard.from_dict(card.to_dict())

    self.assertEqual(restored.status, SkillStatus.ACTIVE)
    self.assertEqual(restored.execution_mode, SkillExecutionMode.AGENT_INTERPRETED)

  def test_plan_patch_serializes_commands(self) -> None:
    patch = PlanPatch(
      patch_id="patch_1",
      run_id="run_1",
      proposed_by_agent_id="agent_1",
      reason="Need a verifier subworkflow.",
      commands=[ExecutionCommand(type="goto", target="verify")],
      status=PlanPatchStatus.PROPOSED,
    )

    restored = PlanPatch.from_dict(patch.to_dict())

    self.assertEqual(restored.commands[0].target, "verify")
    self.assertEqual(restored.status, PlanPatchStatus.PROPOSED)

  def test_exploration_task_serializes_criteria(self) -> None:
    task = ExplorationTask(
      exploration_id="explore_1",
      objective_id="obj_1",
      status=ExplorationStatus.CREATED,
      problem_statement="Find a viable implementation path.",
      acceptance_criteria=[
        AcceptanceCriteria(criteria_id="criteria_1", description="Tests pass")
      ],
    )

    restored = ExplorationTask.from_dict(task.to_dict())

    self.assertEqual(restored.acceptance_criteria[0].description, "Tests pass")
    self.assertEqual(restored.status, ExplorationStatus.CREATED)

  def test_extension_manifest_serializes_contributions(self) -> None:
    manifest = ExtensionManifest(
      extension_id="ext_1",
      name="local tools",
      version="0.1.0",
      compatible_kernel=">=0.1.0",
      contributes=[
        ExtensionContribution(
          kind="tool_provider",
          name="local",
          entrypoint="agent_kernel.capabilities.adapters.local:LocalToolProvider",
        )
      ],
      side_effect_level=SideEffectLevel.EXEC,
    )

    restored = ExtensionManifest.from_dict(manifest.to_dict())

    self.assertEqual(restored.contributes[0].kind, "tool_provider")
    self.assertEqual(restored.side_effect_level, SideEffectLevel.EXEC)

  def test_stability_models_convert_status_enums(self) -> None:
    circuit = CircuitBreakerState(
      circuit_id="circuit_1",
      target_ref="model:mock",
      status="half_open",
      failure_count=2,
    )

    self.assertEqual(circuit.status, CircuitStatus.HALF_OPEN)

  def test_candidate_strategy_is_serializable(self) -> None:
    strategy = CandidateStrategy(
      strategy_id="strategy_1",
      exploration_id="explore_1",
      hypothesis="Try workflow plus local tool.",
      tool_refs=["tool.local.echo"],
    )

    self.assertEqual(strategy.to_dict()["tool_refs"], ["tool.local.echo"])


if __name__ == "__main__":
  unittest.main()

