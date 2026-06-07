"""Workflow node executor protocol and registry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from agent_kernel.domain.workflow import NodeContext, NodeResult


class NodeExecutor(Protocol):
  async def execute(self, ctx: NodeContext) -> NodeResult:
    ...


ExecutorFactory = Callable[[], NodeExecutor]


class NodeExecutorRegistry:
  def __init__(self) -> None:
    self._executors: dict[str, ExecutorFactory] = {}

  def register(self, kind: str, factory: ExecutorFactory) -> None:
    self._executors[kind] = factory

  def resolve(self, kind: str) -> NodeExecutor:
    try:
      return self._executors[kind]()
    except KeyError as exc:
      raise KeyError(f"No node executor registered for kind: {kind}") from exc


class FunctionNodeExecutor:
  def __init__(self, func: Callable[[NodeContext], NodeResult | Awaitable[NodeResult]]) -> None:
    self._func = func

  async def execute(self, ctx: NodeContext) -> NodeResult:
    result = self._func(ctx)
    if hasattr(result, "__await__"):
      return await result  # type: ignore[no-any-return]
    return result

