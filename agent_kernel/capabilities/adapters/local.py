"""Local in-process tool adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_kernel.domain.capability import ToolResult


LocalToolFunc = Callable[[dict[str, Any]], ToolResult | Awaitable[ToolResult]]


class LocalToolExecutor:
  def __init__(self) -> None:
    self._tools: dict[str, LocalToolFunc] = {}

  def register(self, name: str, func: LocalToolFunc) -> None:
    self._tools[name] = func

  async def call(self, name: str, input: dict[str, Any]) -> ToolResult:
    try:
      tool = self._tools[name]
    except KeyError as exc:
      raise KeyError(f"No local tool registered: {name}") from exc
    result = tool(input)
    if hasattr(result, "__await__"):
      return await result  # type: ignore[no-any-return]
    return result

