"""Evaluation suite basics."""

from dataclasses import dataclass, field

from agent_kernel.domain.base import DomainModel
from agent_kernel.evaluation.assertions import AssertionResult, ReplayAssertions
from agent_kernel.evaluation.replay import ReplayResult


@dataclass(slots=True)
class EvalSuiteResult(DomainModel):
  suite_name: str
  assertions: list[AssertionResult] = field(default_factory=list)

  @property
  def passed(self) -> bool:
    return all(assertion.passed for assertion in self.assertions)


class ReplayEvalSuite:
  def __init__(self, suite_name: str = "replay") -> None:
    self.suite_name = suite_name

  def evaluate_completed_run(self, result: ReplayResult) -> EvalSuiteResult:
    return EvalSuiteResult(
      suite_name=self.suite_name,
      assertions=[
        ReplayAssertions.run_status(result, "completed"),
        ReplayAssertions.no_external_calls(result),
      ],
    )

