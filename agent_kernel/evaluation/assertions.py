"""Replay assertions."""

from dataclasses import dataclass

from agent_kernel.domain.base import DomainModel
from agent_kernel.domain.states import RunStatus
from agent_kernel.evaluation.replay import ReplayResult


@dataclass(slots=True)
class AssertionResult(DomainModel):
  name: str
  passed: bool
  message: str | None = None


class ReplayAssertions:
  @staticmethod
  def run_status(result: ReplayResult, expected: RunStatus | str) -> AssertionResult:
    if result.final_state is None:
      return AssertionResult(name="run_status", passed=False, message="No final state.")
    expected_value = expected.value if isinstance(expected, RunStatus) else expected
    actual = result.final_state.status.value
    return AssertionResult(
      name="run_status",
      passed=actual == expected_value,
      message=None if actual == expected_value else f"Expected {expected_value}, got {actual}.",
    )

  @staticmethod
  def event_exists(result: ReplayResult, event_type: str) -> AssertionResult:
    exists = any(event.event_type.value == event_type for event in result.events)
    return AssertionResult(
      name="event_exists",
      passed=exists,
      message=None if exists else f"Event not found: {event_type}",
    )

  @staticmethod
  def no_external_calls(result: ReplayResult) -> AssertionResult:
    return AssertionResult(
      name="no_external_calls",
      passed=result.replayed_external_calls == 0,
      message=None
      if result.replayed_external_calls == 0
      else f"External calls replayed: {result.replayed_external_calls}",
    )
