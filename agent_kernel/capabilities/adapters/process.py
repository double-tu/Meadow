"""Process-backed tool adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import os
import signal
from typing import Literal, Protocol

from agent_kernel.domain.capability import ToolResult


class ProcessIsolationStrategy(Protocol):
  """Platform boundary for process/session isolation and signalling."""

  name: str

  def spawn_kwargs(self) -> dict[str, object]:
    """Return kwargs passed to asyncio.create_subprocess_exec."""

  def terminate(self, process: asyncio.subprocess.Process) -> None:
    """Request graceful termination for the isolated process scope."""

  def kill(self, process: asyncio.subprocess.Process) -> None:
    """Force kill the isolated process scope."""


class SingleProcessIsolationStrategy:
  """Default strategy that only signals the immediate child process."""

  name = "single_process"

  def spawn_kwargs(self) -> dict[str, object]:
    return {}

  def terminate(self, process: asyncio.subprocess.Process) -> None:
    process.terminate()

  def kill(self, process: asyncio.subprocess.Process) -> None:
    process.kill()


class PosixProcessGroupIsolationStrategy:
  """Run each command in a new POSIX session and signal the whole process group."""

  name = "posix_process_group"

  def spawn_kwargs(self) -> dict[str, object]:
    if os.name != "posix":
      return {}
    return {"start_new_session": True}

  def terminate(self, process: asyncio.subprocess.Process) -> None:
    self._signal_group_or_process(process, signal.SIGTERM)

  def kill(self, process: asyncio.subprocess.Process) -> None:
    self._signal_group_or_process(process, signal.SIGKILL)

  @staticmethod
  def _signal_group_or_process(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if os.name != "posix":
      if sig is signal.SIGTERM:
        process.terminate()
      else:
        process.kill()
      return
    try:
      os.killpg(os.getpgid(process.pid), sig)
    except ProcessLookupError:
      return
    except PermissionError:
      if sig is signal.SIGTERM:
        process.terminate()
      else:
        process.kill()


@dataclass(slots=True)
class ProcessCommand:
  argv: list[str]
  timeout_seconds: float | None = None
  isolation_strategy: ProcessIsolationStrategy | None = None


@dataclass(slots=True)
class ProcessStreamEvent:
  event: Literal["started", "stdout", "stderr", "exited", "timeout", "cancelled", "killed"]
  tool_call_id: str | None = None
  process_id: int | None = None
  data: str = ""
  returncode: int | None = None
  error: dict[str, object] | None = None


@dataclass(slots=True)
class ActiveProcess:
  process: asyncio.subprocess.Process
  isolation_strategy: ProcessIsolationStrategy


class ProcessToolExecutor:
  def __init__(self, isolation_strategy: ProcessIsolationStrategy | None = None) -> None:
    self._commands: dict[str, ProcessCommand] = {}
    self._active: dict[str, ActiveProcess] = {}
    self._external_status: dict[str, Literal["cancelled", "killed"]] = {}
    self._isolation_strategy = isolation_strategy or SingleProcessIsolationStrategy()

  def register(
    self,
    name: str,
    argv: list[str],
    timeout_seconds: float | None = None,
    isolation_strategy: ProcessIsolationStrategy | None = None,
  ) -> None:
    self._commands[name] = ProcessCommand(
      argv=argv,
      timeout_seconds=timeout_seconds,
      isolation_strategy=isolation_strategy,
    )

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
    isolation_strategy = command.isolation_strategy or self._isolation_strategy
    process = await asyncio.create_subprocess_exec(
      *argv,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      **isolation_strategy.spawn_kwargs(),
    )
    if tool_call_id is not None:
      self._active[tool_call_id] = ActiveProcess(process, isolation_strategy)
    try:
      stdout, stderr = await asyncio.wait_for(
        process.communicate(),
        timeout=command.timeout_seconds,
      )
    except TimeoutError:
      status = await self._terminate_process(process, isolation_strategy)
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

  async def stream(
    self,
    name: str,
    input: dict[str, object],
    tool_call_id: str | None = None,
  ) -> AsyncIterator[ProcessStreamEvent]:
    try:
      command = self._commands[name]
    except KeyError as exc:
      raise KeyError(f"No process tool registered: {name}") from exc
    argv = [str(part).format(**input) for part in command.argv]
    isolation_strategy = command.isolation_strategy or self._isolation_strategy
    process = await asyncio.create_subprocess_exec(
      *argv,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      **isolation_strategy.spawn_kwargs(),
    )
    if tool_call_id is not None:
      self._active[tool_call_id] = ActiveProcess(process, isolation_strategy)
    yield ProcessStreamEvent(
      event="started",
      tool_call_id=tool_call_id,
      process_id=process.pid,
    )
    queue: asyncio.Queue[ProcessStreamEvent] = asyncio.Queue()
    stdout_task = asyncio.create_task(
      self._pump_stream(process.stdout, "stdout", queue, tool_call_id, process.pid)
    )
    stderr_task = asyncio.create_task(
      self._pump_stream(process.stderr, "stderr", queue, tool_call_id, process.pid)
    )
    wait_task = asyncio.create_task(process.wait())
    timeout_task = (
      asyncio.create_task(asyncio.sleep(command.timeout_seconds))
      if command.timeout_seconds is not None
      else None
    )
    try:
      while True:
        tasks: set[asyncio.Task[object]] = {wait_task}
        if timeout_task is not None:
          tasks.add(timeout_task)
        get_event_task = asyncio.create_task(queue.get())
        tasks.add(get_event_task)
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if get_event_task in done:
          yield get_event_task.result()
        else:
          get_event_task.cancel()
        if timeout_task is not None and timeout_task in done:
          await self._terminate_process(process, isolation_strategy)
          yield ProcessStreamEvent(
            event="timeout",
            tool_call_id=tool_call_id,
            process_id=process.pid,
            returncode=process.returncode,
            error={
              "type": "timeout",
              "message": f"Process timed out after {command.timeout_seconds} seconds.",
            },
          )
          break
        if wait_task in done:
          while not queue.empty():
            yield queue.get_nowait()
          await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
          external_status = self._external_status.pop(tool_call_id, None) if tool_call_id else None
          if external_status in {"cancelled", "killed"}:
            yield ProcessStreamEvent(
              event=external_status,
              tool_call_id=tool_call_id,
              process_id=process.pid,
              returncode=process.returncode,
              error={
                "type": external_status,
                "message": f"Process was externally {external_status}.",
              },
            )
          else:
            yield ProcessStreamEvent(
              event="exited",
              tool_call_id=tool_call_id,
              process_id=process.pid,
              returncode=process.returncode,
              error=None
              if process.returncode == 0
              else {"type": "process_failed", "returncode": process.returncode},
            )
          break
    finally:
      if timeout_task is not None:
        timeout_task.cancel()
      stdout_task.cancel()
      stderr_task.cancel()
      if process.returncode is None:
        isolation_strategy.kill(process)
        await process.wait()
      if tool_call_id is not None:
        self._active.pop(tool_call_id, None)

  async def cancel(self, tool_call_id: str, grace_seconds: float = 1.0) -> str:
    active = self._active.get(tool_call_id)
    if active is None:
      raise KeyError(f"No active process for tool_call_id: {tool_call_id}")
    self._external_status[tool_call_id] = "cancelled"
    if active.process.returncode is not None:
      return "cancelled"
    active.isolation_strategy.terminate(active.process)
    try:
      await asyncio.wait_for(active.process.wait(), timeout=grace_seconds)
      return "cancelled"
    except TimeoutError:
      self._external_status[tool_call_id] = "killed"
      active.isolation_strategy.kill(active.process)
      await active.process.wait()
      return "killed"

  async def kill(self, tool_call_id: str) -> str:
    active = self._active.get(tool_call_id)
    if active is None:
      raise KeyError(f"No active process for tool_call_id: {tool_call_id}")
    self._external_status[tool_call_id] = "killed"
    if active.process.returncode is not None:
      return "killed"
    active.isolation_strategy.kill(active.process)
    await active.process.wait()
    return "killed"

  @staticmethod
  async def _pump_stream(
    reader: asyncio.StreamReader | None,
    event: Literal["stdout", "stderr"],
    queue: asyncio.Queue[ProcessStreamEvent],
    tool_call_id: str | None,
    process_id: int | None,
  ) -> None:
    if reader is None:
      return
    while True:
      data = await reader.readline()
      if not data:
        return
      await queue.put(
        ProcessStreamEvent(
          event=event,
          tool_call_id=tool_call_id,
          process_id=process_id,
          data=data.decode("utf-8", errors="replace"),
        )
      )

  @staticmethod
  async def _terminate_process(
    process: asyncio.subprocess.Process,
    isolation_strategy: ProcessIsolationStrategy,
  ) -> Literal["cancelled", "killed"]:
    if process.returncode is not None:
      return "cancelled"
    isolation_strategy.terminate(process)
    try:
      await asyncio.wait_for(process.wait(), timeout=1)
      return "cancelled"
    except TimeoutError:
      isolation_strategy.kill(process)
      await process.wait()
      return "killed"
