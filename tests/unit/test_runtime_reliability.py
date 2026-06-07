import unittest

from agent_kernel.domain import (
  CircuitBreakerState,
  CircuitStatus,
  NodeStepRecord,
  NodeStepStatus,
  RuntimeBudget,
)
from agent_kernel.runtime.budget import BudgetManager, BudgetUsage
from agent_kernel.runtime.circuit_breaker import CircuitBreaker
from agent_kernel.runtime.lease import Lease
from agent_kernel.runtime.retry import RetryClassifier
from agent_kernel.runtime.scheduler import Scheduler


class RuntimeReliabilityTests(unittest.TestCase):
  def test_scheduler_leases_and_marks_step_running(self) -> None:
    step = NodeStepRecord(
      step_id="step_1",
      run_id="run_1",
      node_id="node_1",
      status=NodeStepStatus.SCHEDULED,
    )
    scheduler = Scheduler()

    leased = scheduler.lease_step(step, owner_id="worker_1")
    running = scheduler.mark_running(leased)

    self.assertEqual(leased.status, NodeStepStatus.LEASED)
    self.assertIsNotNone(leased.lease_id)
    self.assertEqual(running.status, NodeStepStatus.RUNNING)

  def test_lease_expiration(self) -> None:
    lease = Lease.create(owner_id="worker_1", ttl_seconds=-1)

    self.assertTrue(lease.is_expired())

  def test_retry_classifier_distinguishes_transient_and_deterministic(self) -> None:
    classifier = RetryClassifier(max_attempts=2)

    transient = classifier.classify(TimeoutError("timeout calling tool"), attempt=1)
    deterministic = classifier.classify(ValueError("invalid input"), attempt=1)

    self.assertTrue(transient.retryable)
    self.assertEqual(transient.failure_type, "transient")
    self.assertFalse(deterministic.retryable)
    self.assertEqual(deterministic.failure_type, "deterministic")

  def test_budget_manager_reports_exhaustion(self) -> None:
    budget = RuntimeBudget(
      budget_id="budget_1",
      scope="run",
      scope_id="run_1",
      max_tool_calls=2,
    )
    manager = BudgetManager()

    self.assertTrue(manager.check(budget, BudgetUsage(tool_calls=1)))
    self.assertEqual(
      manager.exhausted_reason(budget, BudgetUsage(tool_calls=2)),
      "max_tool_calls exhausted: used 2, limit 2",
    )

  def test_circuit_breaker_opens_after_threshold(self) -> None:
    breaker = CircuitBreaker(failure_threshold=2)
    state = CircuitBreakerState(
      circuit_id="circuit_1",
      target_ref="tool.echo",
      status=CircuitStatus.CLOSED,
    )

    state = breaker.record_failure(state)
    state = breaker.record_failure(state)

    self.assertEqual(state.status, CircuitStatus.OPEN)
    self.assertIsNotNone(state.next_probe_at)


if __name__ == "__main__":
  unittest.main()

