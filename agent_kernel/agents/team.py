"""Agent pool selection."""

from agent_kernel.domain.interaction import AgentPool


class AgentPoolScheduler:
  def select(self, pool: AgentPool) -> str:
    if not pool.agent_session_ids:
      raise ValueError(f"Agent pool is empty: {pool.pool_id}")
    return pool.agent_session_ids[0]

