"""Exploration strategy planner."""

from agent_kernel.domain.base import new_id
from agent_kernel.domain.autonomy import CandidateStrategy, ExplorationTask


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

