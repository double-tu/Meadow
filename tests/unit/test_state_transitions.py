import unittest

from agent_kernel.domain import (
  AgentStatus,
  ApprovalStatus,
  CircuitStatus,
  HumanInterventionStatus,
  NodeStepStatus,
  RecoveryStatus,
  RunStatus,
  TaskStatus,
  ToolCallStatus,
  assert_transition,
  can_transition,
)
from agent_kernel.domain.errors import InvalidStateTransitionError


class StateTransitionTests(unittest.TestCase):
  def test_run_transition_guards(self) -> None:
    self.assertTrue(can_transition(RunStatus.PENDING, RunStatus.RUNNING))
    self.assertTrue(can_transition(RunStatus.RUNNING, RunStatus.PAUSED))
    self.assertTrue(can_transition(RunStatus.PAUSED, RunStatus.RUNNING))

    with self.assertRaises(InvalidStateTransitionError):
      assert_transition(RunStatus.COMPLETED, RunStatus.RUNNING)

  def test_node_step_retry_cycle(self) -> None:
    assert_transition(NodeStepStatus.SCHEDULED, NodeStepStatus.LEASED)
    assert_transition(NodeStepStatus.LEASED, NodeStepStatus.RUNNING)
    assert_transition(NodeStepStatus.RUNNING, NodeStepStatus.RETRY_WAIT)
    assert_transition(NodeStepStatus.RETRY_WAIT, NodeStepStatus.SCHEDULED)

  def test_task_review_and_rework(self) -> None:
    assert_transition(TaskStatus.RUNNING, TaskStatus.REVIEW)
    assert_transition(TaskStatus.REVIEW, TaskStatus.READY)
    assert_transition(TaskStatus.REVIEW, TaskStatus.DONE)

  def test_other_state_machines_have_expected_paths(self) -> None:
    assert_transition(AgentStatus.STARTING, AgentStatus.CANCELLED)
    assert_transition(AgentStatus.IDLE, AgentStatus.RUNNING)
    assert_transition(ApprovalStatus.REQUESTED, ApprovalStatus.APPROVED)
    assert_transition(ToolCallStatus.RUNNING, ToolCallStatus.CANCELLING)
    assert_transition(HumanInterventionStatus.CREATED, HumanInterventionStatus.APPLIED)
    assert_transition(RecoveryStatus.FAILED, RecoveryStatus.PENDING)
    assert_transition(CircuitStatus.HALF_OPEN, CircuitStatus.OPEN)

  def test_mismatched_state_types_are_not_transitionable(self) -> None:
    self.assertFalse(can_transition(RunStatus.RUNNING, TaskStatus.RUNNING))


if __name__ == "__main__":
  unittest.main()
