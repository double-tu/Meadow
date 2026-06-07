"""Model provider protocol."""

from __future__ import annotations

from typing import Protocol

from agent_kernel.domain.context import ModelContext


class ModelProvider(Protocol):
  async def complete(self, model_ref: str, context: ModelContext) -> dict[str, object]:
    ...


class MockModelProvider:
  def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
    self.responses = responses or []
    self.calls: list[tuple[str, ModelContext]] = []

  async def complete(self, model_ref: str, context: ModelContext) -> dict[str, object]:
    self.calls.append((model_ref, context))
    if self.responses:
      return self.responses.pop(0)
    return {"content": ""}

