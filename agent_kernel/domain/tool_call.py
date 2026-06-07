"""Tool call domain model."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.states import ToolCallStatus


@dataclass(slots=True)
class ToolCallRecord(DomainModel):
  tool_call_id: str
  run_id: str
  capability_id: str
  status: ToolCallStatus | str
  idempotency_key: str | None = None
  process_id: int | None = None
  input: dict[str, Any] = field(default_factory=dict)
  output: dict[str, Any] = field(default_factory=dict)
  error: dict[str, Any] | None = None
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = ToolCallStatus(self.status)

