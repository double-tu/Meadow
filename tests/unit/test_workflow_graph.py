import unittest

from agent_kernel.domain import EdgeSpec, NodeSpec, WorkflowSpec
from agent_kernel.workflow import WorkflowGraph, reduce_state


class WorkflowGraphTests(unittest.TestCase):
  def test_returns_first_matching_edge(self) -> None:
    spec = WorkflowSpec(
      workflow_id="wf_1",
      version="0.1.0",
      name="branch",
      input_schema={},
      output_schema={},
      nodes=[
        NodeSpec(node_id="start", kind="transform"),
        NodeSpec(node_id="a", kind="transform"),
        NodeSpec(node_id="b", kind="transform"),
      ],
      edges=[
        EdgeSpec(from_node="start", to_node="a", condition="state.route == 'a'"),
        EdgeSpec(from_node="start", to_node="b"),
      ],
      start_node_id="start",
    )

    graph = WorkflowGraph(spec)

    self.assertEqual(graph.next_node_id("start", {"route": "a"}), "a")
    self.assertEqual(graph.next_node_id("start", {"route": "other"}), "b")

  def test_reduce_state_sets_and_removes_keys(self) -> None:
    self.assertEqual(
      reduce_state({"a": 1, "remove": True}, {"b": 2, "remove": None}),
      {"a": 1, "b": 2},
    )


if __name__ == "__main__":
  unittest.main()

