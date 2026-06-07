"""Capability and tool result domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel
from agent_kernel.domain.events import RuntimeEvent
from agent_kernel.domain.identifiers import ArtifactRef


class SideEffectLevel(StrEnum):
  NONE = "none"
  READ = "read"
  WRITE = "write"
  NETWORK = "network"
  EXEC = "exec"
  EXTERNAL_MUTATION = "external_mutation"


@dataclass(slots=True)
class CapabilitySpec(DomainModel):
  capability_id: str
  name: str
  kind: Literal["tool", "workbench", "workflow", "agent_connector", "human"]
  input_schema: dict[str, Any]
  output_schema: dict[str, Any]
  side_effect_level: SideEffectLevel | str
  timeout_seconds: int | None = None
  supports_streaming: bool = False
  supports_idempotency: bool = False
  required_grant: str | None = None

  def __post_init__(self) -> None:
    if isinstance(self.side_effect_level, str):
      self.side_effect_level = SideEffectLevel(self.side_effect_level)


@dataclass(slots=True)
class CapabilityGrant(DomainModel):
  grant_id: str
  capability_id: str
  expires_at: datetime
  agent_id: str | None = None
  task_id: str | None = None
  run_id: str | None = None
  workspace_scope: str | None = None
  filesystem_scope: list[str] = field(default_factory=list)
  network_scope: list[str] = field(default_factory=list)
  secret_scope: list[str] = field(default_factory=list)
  approval_required: bool = False
  max_cost_usd: float | None = None


@dataclass(slots=True)
class ToolResult(DomainModel):
  ok: bool
  output: dict[str, Any] = field(default_factory=dict)
  error: dict[str, Any] | None = None
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  events: list[RuntimeEvent] = field(default_factory=list)

