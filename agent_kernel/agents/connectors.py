"""Agent connector protocol for persistent external sessions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
import json
import sys
from typing import Any, Literal, Protocol

from agent_kernel.domain.base import new_id


@dataclass(slots=True)
class ConnectorMessage:
  message_id: str
  session_id: str
  content: dict[str, Any]


@dataclass(slots=True)
class ConnectorTurn:
  turn_id: str
  session_id: str
  output: dict[str, Any]
  completed: bool = False


class AgentConnector(Protocol):
  async def start(self, session_id: str, metadata: dict[str, Any] | None = None) -> None:
    ...

  async def send(self, message: ConnectorMessage) -> ConnectorTurn:
    ...

  async def stop(self, session_id: str, reason: str) -> None:
    ...


@dataclass(slots=True)
class ConnectorRoute:
  participant_id: str
  session_id: str
  connector_id: str
  metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class RoutedConnectorTurn:
  participant_id: str
  route: ConnectorRoute
  turn: ConnectorTurn
  channel_message_id: str | None = None


class FakeAgentConnector:
  def __init__(self) -> None:
    self.started: list[str] = []
    self.stopped: list[tuple[str, str]] = []
    self.messages: list[ConnectorMessage] = []
    self.responses: list[ConnectorTurn] = []

  def queue_response(self, turn: ConnectorTurn) -> None:
    self.responses.append(turn)

  async def start(self, session_id: str, metadata: dict[str, Any] | None = None) -> None:
    self.started.append(session_id)

  async def send(self, message: ConnectorMessage) -> ConnectorTurn:
    self.messages.append(message)
    if self.responses:
      return self.responses.pop(0)
    return ConnectorTurn(
      turn_id=f"turn_{message.message_id}",
      session_id=message.session_id,
      output={"echo": message.content},
      completed=False,
    )

  async def stop(self, session_id: str, reason: str) -> None:
    self.stopped.append((session_id, reason))


@dataclass(slots=True)
class StdioAgentCommand:
  argv: list[str]
  cwd: str | None = None
  startup_timeout_seconds: float = 5.0
  turn_timeout_seconds: float = 60.0


@dataclass(slots=True)
class ProductCLIConnectorSpec:
  connector_id: str
  product: str
  argv: list[str]
  cwd: str | None = None
  startup_timeout_seconds: float = 5.0
  turn_timeout_seconds: float = 60.0
  metadata: dict[str, Any] | None = None

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> "ProductCLIConnectorSpec":
    argv = data.get("argv")
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
      raise ValueError("Product CLI connector spec requires argv as a list of strings.")
    connector_id = data.get("connector_id")
    product = data.get("product")
    if not isinstance(connector_id, str) or not connector_id:
      raise ValueError("Product CLI connector spec requires connector_id.")
    if not isinstance(product, str) or not product:
      raise ValueError("Product CLI connector spec requires product.")
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
      raise ValueError("Product CLI connector spec metadata must be an object.")
    return cls(
      connector_id=connector_id,
      product=product,
      argv=argv,
      cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else None,
      startup_timeout_seconds=float(data.get("startup_timeout_seconds", 5.0)),
      turn_timeout_seconds=float(data.get("turn_timeout_seconds", 60.0)),
      metadata=metadata,
    )

  def to_command(self) -> StdioAgentCommand:
    return StdioAgentCommand(
      argv=self.argv,
      cwd=self.cwd,
      startup_timeout_seconds=self.startup_timeout_seconds,
      turn_timeout_seconds=self.turn_timeout_seconds,
    )


@dataclass(slots=True)
class ProductCLIShimProfile:
  """Builds a JSONL shim command for a concrete product CLI.

  The profile owns product-specific command-line details while the connector
  still speaks Meadow's stable JSONL protocol.
  """

  product: Literal["codex", "claude", "gemini"] | str
  executable: str
  default_args: list[str] = field(default_factory=list)
  prompt_mode: Literal["stdin", "argument", "json_stdin"] = "stdin"
  prompt_argument: str | None = None
  output_format: Literal["text", "json"] = "text"
  request_timeout_seconds: float = 120.0

  @classmethod
  def codex(
    cls,
    executable: str = "codex",
    default_args: list[str] | None = None,
    **kwargs: Any,
  ) -> "ProductCLIShimProfile":
    return cls(product="codex", executable=executable, default_args=default_args or [], **kwargs)

  @classmethod
  def claude(
    cls,
    executable: str = "claude",
    default_args: list[str] | None = None,
    **kwargs: Any,
  ) -> "ProductCLIShimProfile":
    return cls(product="claude", executable=executable, default_args=default_args or [], **kwargs)

  @classmethod
  def gemini(
    cls,
    executable: str = "gemini",
    default_args: list[str] | None = None,
    **kwargs: Any,
  ) -> "ProductCLIShimProfile":
    return cls(product="gemini", executable=executable, default_args=default_args or [], **kwargs)

  def to_connector_spec(
    self,
    connector_id: str,
    cwd: str | None = None,
    startup_timeout_seconds: float = 5.0,
    turn_timeout_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
  ) -> ProductCLIConnectorSpec:
    return ProductCLIConnectorSpec(
      connector_id=connector_id,
      product=str(self.product),
      argv=self.to_shim_argv(),
      cwd=cwd,
      startup_timeout_seconds=startup_timeout_seconds,
      turn_timeout_seconds=turn_timeout_seconds or max(60.0, self.request_timeout_seconds + 5.0),
      metadata={
        "shim": "agent_kernel.agents.cli_shim",
        "prompt_mode": self.prompt_mode,
        **(metadata or {}),
      },
    )

  def to_shim_argv(self) -> list[str]:
    argv = [
      sys.executable,
      "-u",
      "-m",
      "agent_kernel.agents.cli_shim",
      "--product",
      str(self.product),
      "--executable",
      self.executable,
      "--prompt-mode",
      self.prompt_mode,
      "--output-format",
      self.output_format,
      "--timeout-seconds",
      str(self.request_timeout_seconds),
    ]
    if self.prompt_argument is not None:
      argv.extend(["--prompt-argument", self.prompt_argument])
    if self.default_args:
      argv.extend(["--args-json", json.dumps(self.default_args, ensure_ascii=False)])
    return argv


class StructuredStdioAgentConnector:
  """Persistent JSONL stdio connector for external agent session shims."""

  def __init__(self, command: StdioAgentCommand) -> None:
    self._command = command
    self._processes: dict[str, asyncio.subprocess.Process] = {}

  async def start(self, session_id: str, metadata: dict[str, Any] | None = None) -> None:
    if session_id in self._processes:
      return
    process = await asyncio.create_subprocess_exec(
      *self._command.argv,
      cwd=self._command.cwd,
      stdin=asyncio.subprocess.PIPE,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
    )
    self._processes[session_id] = process
    await self._write_json(process, {"type": "start", "session_id": session_id, "metadata": metadata or {}})
    response = await self._read_json(process, timeout=self._command.startup_timeout_seconds)
    if response.get("type") != "started":
      await self.stop(session_id, "invalid startup response")
      raise RuntimeError(f"Invalid connector startup response: {response!r}")

  async def send(self, message: ConnectorMessage) -> ConnectorTurn:
    process = self._get_process(message.session_id)
    await self._write_json(
      process,
      {
        "type": "message",
        "message_id": message.message_id,
        "session_id": message.session_id,
        "content": message.content,
      },
    )
    response = await self._read_json(process, timeout=self._command.turn_timeout_seconds)
    if response.get("type") != "turn":
      raise RuntimeError(f"Invalid connector turn response: {response!r}")
    output = response.get("output")
    if not isinstance(output, dict):
      raise RuntimeError("Connector turn response must contain object output.")
    turn_id = response.get("turn_id")
    return ConnectorTurn(
      turn_id=turn_id if isinstance(turn_id, str) else new_id("connector_turn"),
      session_id=message.session_id,
      output=output,
      completed=bool(response.get("completed", False)),
    )

  async def stop(self, session_id: str, reason: str) -> None:
    process = self._processes.pop(session_id, None)
    if process is None:
      return
    try:
      await self._write_json(process, {"type": "stop", "session_id": session_id, "reason": reason})
    except RuntimeError:
      pass
    try:
      await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
      process.terminate()
      try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
      except TimeoutError:
        process.kill()
        await process.wait()

  def _get_process(self, session_id: str) -> asyncio.subprocess.Process:
    try:
      process = self._processes[session_id]
    except KeyError as exc:
      raise KeyError(f"Connector session not started: {session_id}") from exc
    if process.returncode is not None:
      raise RuntimeError(f"Connector session already exited: {session_id}")
    return process

  @staticmethod
  async def _write_json(process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
    if process.stdin is None:
      raise RuntimeError("Connector stdin is not available.")
    process.stdin.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
    await process.stdin.drain()

  @staticmethod
  async def _read_json(process: asyncio.subprocess.Process, timeout: float) -> dict[str, Any]:
    if process.stdout is None:
      raise RuntimeError("Connector stdout is not available.")
    line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
    if not line:
      stderr = ""
      if process.stderr is not None:
        stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
      raise RuntimeError(f"Connector closed stdout. stderr={stderr}")
    try:
      value = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
      raise RuntimeError("Connector emitted invalid JSON.") from exc
    if not isinstance(value, dict):
      raise RuntimeError("Connector JSONL frame must be an object.")
    return value


class ProductCLIConnectorFactory:
  """Builds structured stdio connectors from product CLI shim specs."""

  def build(self, spec: ProductCLIConnectorSpec) -> StructuredStdioAgentConnector:
    return StructuredStdioAgentConnector(spec.to_command())

  def build_profile(
    self,
    connector_id: str,
    profile: ProductCLIShimProfile,
    cwd: str | None = None,
    startup_timeout_seconds: float = 5.0,
    turn_timeout_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
  ) -> StructuredStdioAgentConnector:
    return self.build(
      profile.to_connector_spec(
        connector_id=connector_id,
        cwd=cwd,
        startup_timeout_seconds=startup_timeout_seconds,
        turn_timeout_seconds=turn_timeout_seconds,
        metadata=metadata,
      )
    )

  def build_many(
    self,
    specs: list[ProductCLIConnectorSpec],
  ) -> dict[str, StructuredStdioAgentConnector]:
    connectors: dict[str, StructuredStdioAgentConnector] = {}
    for spec in specs:
      if spec.connector_id in connectors:
        raise ValueError(f"Duplicate connector_id: {spec.connector_id}")
      connectors[spec.connector_id] = self.build(spec)
    return connectors


class AgentConnectorRouter:
  """Routes participant messages to persistent external agent sessions."""

  def __init__(self, connectors: dict[str, AgentConnector], fabric=None) -> None:
    self._connectors = connectors
    self._fabric = fabric
    self._routes: dict[str, ConnectorRoute] = {}

  def bind(self, route: ConnectorRoute) -> ConnectorRoute:
    if route.connector_id not in self._connectors:
      raise KeyError(f"Connector not registered: {route.connector_id}")
    self._routes[route.participant_id] = route
    return route

  def get_route(self, participant_id: str) -> ConnectorRoute:
    try:
      return self._routes[participant_id]
    except KeyError as exc:
      raise KeyError(f"No connector route for participant: {participant_id}") from exc

  async def start_all(self) -> None:
    for route in self._routes.values():
      await self._connectors[route.connector_id].start(route.session_id, route.metadata)

  async def send_to_participant(
    self,
    participant_id: str,
    content: dict[str, Any],
    channel_id: str | None = None,
  ) -> RoutedConnectorTurn:
    route = self.get_route(participant_id)
    message = ConnectorMessage(
      message_id=new_id("connector_msg"),
      session_id=route.session_id,
      content=content,
    )
    turn = await self._connectors[route.connector_id].send(message)
    channel_message_id = None
    if self._fabric is not None and channel_id is not None:
      interaction_message = self._fabric.send_message(
        channel_id,
        participant_id,
        {
          "type": "connector_turn",
          "connector_id": route.connector_id,
          "session_id": route.session_id,
          "turn_id": turn.turn_id,
          "output": turn.output,
          "completed": turn.completed,
        },
      )
      channel_message_id = interaction_message.message_id
    return RoutedConnectorTurn(
      participant_id=participant_id,
      route=route,
      turn=turn,
      channel_message_id=channel_message_id,
    )

  async def stop_all(self, reason: str) -> None:
    for route in self._routes.values():
      await self._connectors[route.connector_id].stop(route.session_id, reason)
