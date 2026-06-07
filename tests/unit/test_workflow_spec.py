import unittest

from agent_kernel.domain import EdgeSpec, NodeSpec, WorkflowSpec
from agent_kernel.domain.errors import DomainValidationError


class WorkflowSpecTests(unittest.TestCase):
  def test_valid_workflow_spec(self) -> None:
    spec = WorkflowSpec(
      workflow_id="wf_1",
      version="0.1.0",
      name="three node workflow",
      input_schema={},
      output_schema={},
      nodes=[
        NodeSpec(node_id="start", kind="transform"),
        NodeSpec(node_id="work", kind="tool", capability_ref="tool.echo"),
        NodeSpec(node_id="finish", kind="transform"),
      ],
      edges=[
        EdgeSpec(from_node="start", to_node="work"),
        EdgeSpec(from_node="work", to_node="finish"),
      ],
      start_node_id="start",
    )

    self.assertEqual(spec.nodes[1].capability_ref, "tool.echo")

  def test_rejects_missing_start_node(self) -> None:
    with self.assertRaises(DomainValidationError):
      WorkflowSpec(
        workflow_id="wf_1",
        version="0.1.0",
        name="bad workflow",
        input_schema={},
        output_schema={},
        nodes=[NodeSpec(node_id="only", kind="transform")],
        edges=[],
        start_node_id="missing",
      )

  def test_rejects_edge_to_missing_node(self) -> None:
    with self.assertRaises(DomainValidationError):
      WorkflowSpec(
        workflow_id="wf_1",
        version="0.1.0",
        name="bad workflow",
        input_schema={},
        output_schema={},
        nodes=[NodeSpec(node_id="start", kind="transform")],
        edges=[EdgeSpec(from_node="start", to_node="missing")],
        start_node_id="start",
      )


if __name__ == "__main__":
  unittest.main()

