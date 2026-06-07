"""Exploration strategy planner."""

from typing import Protocol

from agent_kernel.domain.base import new_id
from agent_kernel.domain.autonomy import CandidateStrategy, ExplorationTask


class StrategyGenerator(Protocol):
  def generate(self, task: ExplorationTask) -> list[CandidateStrategy]:
    ...


class ExplorationPlanner:
  def generate(self, task: ExplorationTask) -> list[CandidateStrategy]:
    return [
      CandidateStrategy(
        strategy_id=new_id("strategy"),
        exploration_id=task.exploration_id,
        hypothesis=f"Attempt direct solution for: {task.problem_statement}",
        priority=1,
      )
    ]


class DirectSolutionStrategyGenerator:
  def generate(self, task: ExplorationTask) -> list[CandidateStrategy]:
    return [
      CandidateStrategy(
        strategy_id=new_id("strategy"),
        exploration_id=task.exploration_id,
        hypothesis=f"Attempt direct solution for: {task.problem_statement}",
        priority=30,
      )
    ]


class DiscoveryFirstStrategyGenerator:
  def generate(self, task: ExplorationTask) -> list[CandidateStrategy]:
    return [
      CandidateStrategy(
        strategy_id=new_id("strategy"),
        exploration_id=task.exploration_id,
        hypothesis=f"Discover constraints and available tools before solving: {task.problem_statement}",
        tool_refs=["tool.inspect_context"],
        expected_artifacts=["constraint_report"],
        risk_notes=["low-risk discovery before side effects"],
        priority=20,
      )
    ]


class WorkflowReuseStrategyGenerator:
  def __init__(self, workflow_refs: list[str] | None = None) -> None:
    self._workflow_refs = workflow_refs or ["workflow://library/search"]

  def generate(self, task: ExplorationTask) -> list[CandidateStrategy]:
    return [
      CandidateStrategy(
        strategy_id=new_id("strategy"),
        exploration_id=task.exploration_id,
        hypothesis=f"Reuse an existing workflow template for: {task.problem_statement}",
        workflow_refs=list(self._workflow_refs),
        expected_artifacts=["workflow_reuse_report"],
        priority=10,
      )
    ]


class CompositeExplorationPlanner:
  """Combines independent strategy generators and normalizes priorities."""

  def __init__(self, generators: list[StrategyGenerator] | None = None) -> None:
    self._generators = generators or [
      DirectSolutionStrategyGenerator(),
      DiscoveryFirstStrategyGenerator(),
      WorkflowReuseStrategyGenerator(),
    ]

  def generate(self, task: ExplorationTask) -> list[CandidateStrategy]:
    strategies: list[CandidateStrategy] = []
    seen: set[str] = set()
    for generator in self._generators:
      for strategy in generator.generate(task):
        key = strategy.hypothesis.strip().lower()
        if key in seen:
          continue
        seen.add(key)
        strategies.append(strategy)
    return sorted(strategies, key=lambda item: item.priority, reverse=True)
