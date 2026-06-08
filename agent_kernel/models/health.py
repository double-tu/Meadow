"""Model health and fallback routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ModelFailureKind(StrEnum):
  TIMEOUT = "timeout"
  EMPTY_OUTPUT = "empty_output"
  PROTOCOL_ERROR = "protocol_error"
  TOOL_PROTOCOL_INCOMPATIBLE = "tool_protocol_incompatible"
  MAX_TOKENS = "max_tokens"
  PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class ModelRoute:
  provider_name: str
  model_ref: str

  @property
  def key(self) -> str:
    return f"{self.provider_name}:{self.model_ref}"


@dataclass(slots=True)
class ModelHealthRecord:
  route: ModelRoute
  failures: dict[ModelFailureKind, int] = field(default_factory=dict)
  successes: int = 0
  consecutive_failures: int = 0
  metadata: dict[str, Any] = field(default_factory=dict)

  @property
  def score(self) -> int:
    weighted = 0
    weights = {
      ModelFailureKind.TIMEOUT: 3,
      ModelFailureKind.EMPTY_OUTPUT: 2,
      ModelFailureKind.PROTOCOL_ERROR: 3,
      ModelFailureKind.TOOL_PROTOCOL_INCOMPATIBLE: 4,
      ModelFailureKind.MAX_TOKENS: 1,
      ModelFailureKind.PROVIDER_ERROR: 3,
    }
    for kind, count in self.failures.items():
      weighted += weights[kind] * count
    return weighted + self.consecutive_failures * 2 - self.successes


class ModelHealthRouter:
  """Chooses a healthy route while keeping routing policy outside providers."""

  def __init__(self, fallbacks: dict[str, list[ModelRoute]] | None = None, failure_threshold: int = 3) -> None:
    self._fallbacks = fallbacks or {}
    self._failure_threshold = failure_threshold
    self._records: dict[str, ModelHealthRecord] = {}

  def choose(self, primary: ModelRoute) -> ModelRoute:
    candidates = [primary, *self._fallbacks.get(primary.key, [])]
    return min(candidates, key=lambda route: self._record(route).score)

  def should_retry_with_fallback(self, primary: ModelRoute) -> bool:
    return self._record(primary).consecutive_failures >= self._failure_threshold

  def record_success(self, route: ModelRoute, metadata: dict[str, Any] | None = None) -> None:
    record = self._record(route)
    record.successes += 1
    record.consecutive_failures = 0
    if metadata:
      record.metadata.update(metadata)

  def record_failure(
    self,
    route: ModelRoute,
    kind: ModelFailureKind | str,
    metadata: dict[str, Any] | None = None,
  ) -> None:
    if isinstance(kind, str):
      kind = ModelFailureKind(kind)
    record = self._record(route)
    record.failures[kind] = record.failures.get(kind, 0) + 1
    record.consecutive_failures += 1
    if metadata:
      record.metadata.update(metadata)

  def snapshot(self) -> list[ModelHealthRecord]:
    return list(self._records.values())

  def _record(self, route: ModelRoute) -> ModelHealthRecord:
    if route.key not in self._records:
      self._records[route.key] = ModelHealthRecord(route=route)
    return self._records[route.key]


def classify_model_result(result: dict[str, object]) -> ModelFailureKind | None:
  finish = result.get("finish_reason") or result.get("stop_reason") or result.get("finish")
  if isinstance(finish, str) and finish.lower() in {"max_tokens", "length"}:
    return ModelFailureKind.MAX_TOKENS
  errors = result.get("protocol_errors")
  if isinstance(errors, list) and errors:
    return ModelFailureKind.PROTOCOL_ERROR
  output = result.get("output", result.get("content"))
  if output is None:
    return ModelFailureKind.EMPTY_OUTPUT
  if isinstance(output, str) and not output.strip() and not result.get("tool_calls"):
    return ModelFailureKind.EMPTY_OUTPUT
  return None
