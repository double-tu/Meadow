"""Distill successful attempts into golden traces."""

from agent_kernel.domain.autonomy import ExplorationAttempt, GoldenTrace
from agent_kernel.domain.base import new_id


class TraceDistiller:
  def distill(self, attempt: ExplorationAttempt) -> GoldenTrace:
    return GoldenTrace(
      trace_id=new_id("trace"),
      source_run_id=attempt.run_id,
      source_attempt_id=attempt.attempt_id,
      event_refs=attempt.event_refs,
      artifact_refs=attempt.artifact_refs,
      verified_by="verifier",
    )

