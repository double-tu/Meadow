"""Agent connector protocol for persistent external sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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
