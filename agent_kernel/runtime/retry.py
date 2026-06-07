"""Retry classification and backoff policy."""

from dataclasses import dataclass
from typing import Literal


FailureType = Literal["deterministic", "transient", "policy", "model", "tool", "system"]


@dataclass(slots=True)
class RetryDecision:
  retryable: bool
  failure_type: FailureType
  next_attempt: int
  delay_seconds: float = 0
  reason: str | None = None


class RetryClassifier:
  def __init__(self, max_attempts: int = 3, base_delay_seconds: float = 0.1) -> None:
    self.max_attempts = max_attempts
    self.base_delay_seconds = base_delay_seconds

  def classify(self, exc: BaseException, attempt: int) -> RetryDecision:
    message = str(exc)
    failure_type = self._classify_failure(message)
    retryable = failure_type in {"transient", "model", "tool", "system"} and attempt < self.max_attempts
    delay = self.base_delay_seconds * (2 ** max(attempt - 1, 0)) if retryable else 0
    return RetryDecision(
      retryable=retryable,
      failure_type=failure_type,
      next_attempt=attempt + 1,
      delay_seconds=delay,
      reason=message,
    )

  @staticmethod
  def _classify_failure(message: str) -> FailureType:
    lowered = message.lower()
    if "policy" in lowered or "permission" in lowered or "denied" in lowered:
      return "policy"
    if "timeout" in lowered or "rate limit" in lowered or "temporar" in lowered:
      return "transient"
    if "model" in lowered:
      return "model"
    if "tool" in lowered:
      return "tool"
    if "system" in lowered or "storage" in lowered:
      return "system"
    return "deterministic"

