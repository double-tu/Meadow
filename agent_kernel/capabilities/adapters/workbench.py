"""Workbench adapter protocol and fake implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class WorkbenchCommand:
  command_id: str
  kind: str
  payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkbenchResult:
  ok: bool
  output: dict[str, Any] = field(default_factory=dict)
  error: dict[str, Any] | None = None


class WorkbenchClient(Protocol):
  async def execute(self, command: WorkbenchCommand) -> WorkbenchResult:
    ...


class FakeWorkbenchClient:
  def __init__(self) -> None:
    self.responses: dict[str, WorkbenchResult] = {}
    self.commands: list[WorkbenchCommand] = []

  def register_response(self, kind: str, result: WorkbenchResult) -> None:
    self.responses[kind] = result

  async def execute(self, command: WorkbenchCommand) -> WorkbenchResult:
    self.commands.append(command)
    return self.responses.get(
      command.kind,
      WorkbenchResult(ok=False, error={"type": "workbench_command_not_found", "kind": command.kind}),
    )
