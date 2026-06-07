"""Process-backed tool adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from agent_kernel.domain.capability import ToolResult


@dataclass(slots=True)
class ProcessCommand:
  argv: list[str]
  timeout_seconds: float | None = None


class ProcessToolExecutor:
  def __init__(self) -> None:
    self._commands: dict[str, ProcessCommand] = {}
    self._active: dict[str, asyncio.subprocess.Process] = {}
    self._external_status: dict[str, Literal["cancelled", "killed"]] = {}

  def register(self, name: str, argv: list[str], timeout_seconds: float | None = None) -> None:
    self._commands[name] = ProcessCommand(argv=argv, timeout_seconds=timeout_seconds)

  async def call(
    self,
    name: str,
    input: dict[str, object],
    tool_call_id: str | None = None,
  ) -> ToolResult:
    try:
      command = self._commands[name]
    except KeyError as exc:
      raise KeyError(f"No process tool registered: {name}") from exc
    argv = [str(part).format(**input) for part in command.argv]
    process = await asyncio.create_subprocess_exec(
      *argv,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
    )
    if tool_call_id is not None:
      self._active[tool_call_id] = process
    try:
      stdout, stderr = await asyncio.wait_for(
        process.communicate(),
        timeout=command.timeout_seconds,
      )
    except TimeoutError:
      process.terminate()
      try:
        await asyncio.wait_for(process.wait(), timeout=1)
        status = "cancelled"
      except TimeoutError:
        process.kill()
        await process.wait()
        status = "killed"
      return ToolResult(
        ok=False,
        error={
          "type": "timeout",
          "message": f"Process timed out after {command.timeout_seconds} seconds.",
          "status": status,
          "process_id": process.pid,
        },
      )
    finally:
      if tool_call_id is not None:
        self._active.pop(tool_call_id, None)
    external_status = self._external_status.pop(tool_call_id, None) if tool_call_id else None
    if external_status is not None:
      return ToolResult(
        ok=False,
        error={
          "type": external_status,
          "message": f"Process was externally {external_status}.",
          "process_id": process.pid,
        },
      )
    return ToolResult(
      ok=process.returncode == 0,
      output={
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "returncode": process.returncode,
        "process_id": process.pid,
      },
      error=None
      if process.returncode == 0
      else {
        "type": "process_failed",
        "returncode": process.returncode,
        "stderr": stderr.decode("utf-8", errors="replace"),
      },
    )

  async def cancel(self, tool_call_id: str, grace_seconds: float = 1.0) -> str:
    process = self._active.get(tool_call_id)
    if process is None:
      raise KeyError(f"No active process for tool_call_id: {tool_call_id}")
    self._external_status[tool_call_id] = "cancelled"
    process.terminate()
    try:
      await asyncio.wait_for(process.wait(), timeout=grace_seconds)
      return "cancelled"
    except TimeoutError:
      self._external_status[tool_call_id] = "killed"
      process.kill()
      await process.wait()
      return "killed"

  async def kill(self, tool_call_id: str) -> str:
    process = self._active.get(tool_call_id)
    if process is None:
      raise KeyError(f"No active process for tool_call_id: {tool_call_id}")
    self._external_status[tool_call_id] = "killed"
    process.kill()
    await process.wait()
    return "killed"
