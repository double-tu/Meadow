"""Runtime budget accounting."""

from dataclasses import dataclass

from agent_kernel.domain.stability import RuntimeBudget


@dataclass(slots=True)
class BudgetUsage:
  model_calls: int = 0
  tool_calls: int = 0
  spawned_agents: int = 0
  tokens: int = 0
  cost_usd: float = 0.0
  wall_time_seconds: int = 0


class BudgetManager:
  def check(self, budget: RuntimeBudget, usage: BudgetUsage) -> bool:
    return self.exhausted_reason(budget, usage) is None

  def exhausted_reason(self, budget: RuntimeBudget, usage: BudgetUsage) -> str | None:
    limits = [
      ("max_model_calls", budget.max_model_calls, usage.model_calls),
      ("max_tool_calls", budget.max_tool_calls, usage.tool_calls),
      ("max_spawned_agents", budget.max_spawned_agents, usage.spawned_agents),
      ("max_tokens", budget.max_tokens, usage.tokens),
      ("max_cost_usd", budget.max_cost_usd, usage.cost_usd),
      ("max_wall_time_seconds", budget.max_wall_time_seconds, usage.wall_time_seconds),
    ]
    for name, limit, used in limits:
      if limit is not None and used >= limit:
        return f"{name} exhausted: used {used}, limit {limit}"
    return None

