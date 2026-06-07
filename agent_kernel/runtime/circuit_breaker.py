"""Circuit breaker state transitions."""

from dataclasses import replace
from datetime import timedelta

from agent_kernel.domain.base import utc_now
from agent_kernel.domain.stability import CircuitBreakerState
from agent_kernel.domain.states import CircuitStatus, assert_transition


class CircuitBreaker:
  def __init__(self, failure_threshold: int = 3, reset_timeout_seconds: int = 30) -> None:
    self.failure_threshold = failure_threshold
    self.reset_timeout_seconds = reset_timeout_seconds

  def record_success(self, state: CircuitBreakerState) -> CircuitBreakerState:
    if state.status is CircuitStatus.HALF_OPEN:
      assert_transition(state.status, CircuitStatus.CLOSED)
      return replace(state, status=CircuitStatus.CLOSED, failure_count=0, opened_at=None, next_probe_at=None)
    if state.status is CircuitStatus.CLOSED:
      return replace(state, failure_count=0)
    return state

  def record_failure(self, state: CircuitBreakerState) -> CircuitBreakerState:
    failure_count = state.failure_count + 1
    if state.status is CircuitStatus.HALF_OPEN:
      assert_transition(state.status, CircuitStatus.OPEN)
      return self._open(replace(state, failure_count=failure_count))
    if state.status is CircuitStatus.CLOSED and failure_count >= self.failure_threshold:
      assert_transition(state.status, CircuitStatus.OPEN)
      return self._open(replace(state, failure_count=failure_count))
    return replace(state, failure_count=failure_count)

  def maybe_probe(self, state: CircuitBreakerState) -> CircuitBreakerState:
    if state.status is not CircuitStatus.OPEN:
      return state
    if state.next_probe_at is not None and utc_now() >= state.next_probe_at:
      assert_transition(state.status, CircuitStatus.HALF_OPEN)
      return replace(state, status=CircuitStatus.HALF_OPEN)
    return state

  def _open(self, state: CircuitBreakerState) -> CircuitBreakerState:
    now = utc_now()
    return replace(
      state,
      status=CircuitStatus.OPEN,
      opened_at=now,
      next_probe_at=now + timedelta(seconds=self.reset_timeout_seconds),
    )

