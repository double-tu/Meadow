"""Workflow graph helper."""

from __future__ import annotations

from agent_kernel.domain.errors import DomainValidationError
from agent_kernel.domain.workflow import EdgeSpec, NodeSpec, WorkflowSpec


class WorkflowGraph:
  def __init__(self, spec: WorkflowSpec) -> None:
    self.spec = spec
    self._nodes = {node.node_id: node for node in spec.nodes}
    self._outgoing: dict[str, list[EdgeSpec]] = {}
    for edge in spec.edges:
      self._outgoing.setdefault(edge.from_node, []).append(edge)

  def get_node(self, node_id: str) -> NodeSpec:
    try:
      return self._nodes[node_id]
    except KeyError as exc:
      raise DomainValidationError(f"Workflow node does not exist: {node_id}") from exc

  def next_node_id(self, current_node_id: str, state: dict[str, object]) -> str | None:
    edges = self._outgoing.get(current_node_id, [])
    if not edges:
      return None
    for edge in edges:
      if _condition_matches(edge.condition, state):
        return edge.to_node
    return None


def _condition_matches(condition: str | None, state: dict[str, object]) -> bool:
  if condition is None:
    return True
  if condition.startswith("state."):
    key, _, expected = condition.removeprefix("state.").partition("==")
    if not expected:
      return bool(state.get(key.strip()))
    return str(state.get(key.strip())) == expected.strip().strip("'\"")
  raise DomainValidationError(f"Unsupported workflow edge condition: {condition}")

