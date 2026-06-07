"""Exploration verifier."""

from dataclasses import dataclass


@dataclass(slots=True)
class VerificationResult:
  ok: bool
  summary: str


class ExplorationVerifier:
  def verify(self, result: dict[str, object]) -> VerificationResult:
    ok = bool(result.get("ok"))
    return VerificationResult(ok=ok, summary=str(result.get("summary", "")))

