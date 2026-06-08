"""Capability and tool result domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.events import RuntimeEvent
from agent_kernel.domain.identifiers import ArtifactRef


class SideEffectLevel(StrEnum):
  NONE = "none"
  READ = "read"
  WRITE = "write"
  NETWORK = "network"
  EXEC = "exec"
  EXTERNAL_MUTATION = "external_mutation"


class CapabilityConcurrency(StrEnum):
  SAFE = "safe"
  OWNER_SCOPED = "owner_scoped"
  EXCLUSIVE = "exclusive"


class CapabilityInterruptBehavior(StrEnum):
  CONTINUE = "continue"
  CANCEL_CHILDREN = "cancel_children"
  REQUIRE_RESUME = "require_resume"
  TERMINAL = "terminal"


@dataclass(slots=True)
class CapabilityRenderHint(DomainModel):
  display_name: str | None = None
  progress_label: str | None = None
  result_label: str | None = None
  icon: str | None = None
  collapsed_by_default: bool = True


@dataclass(slots=True)
class CapabilityExecutionPolicy(DomainModel):
  read_only: bool = False
  destructive: bool = False
  concurrency: CapabilityConcurrency | str = CapabilityConcurrency.EXCLUSIVE
  requires_user_interaction: bool = False
  interrupt_behavior: CapabilityInterruptBehavior | str = CapabilityInterruptBehavior.CONTINUE
  progress_schema: dict[str, Any] = field(default_factory=dict)
  output_compaction_policy: dict[str, Any] = field(default_factory=dict)
  permission_preview: dict[str, Any] = field(default_factory=dict)
  resource_locks: list[str] = field(default_factory=list)
  render_hint: CapabilityRenderHint = field(default_factory=CapabilityRenderHint)

  def __post_init__(self) -> None:
    if isinstance(self.concurrency, str):
      self.concurrency = CapabilityConcurrency(self.concurrency)
    if isinstance(self.interrupt_behavior, str):
      self.interrupt_behavior = CapabilityInterruptBehavior(self.interrupt_behavior)

  @property
  def concurrency_safe(self) -> bool:
    return self.concurrency is CapabilityConcurrency.SAFE


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
  execution_policy: CapabilityExecutionPolicy = field(default_factory=CapabilityExecutionPolicy)

  def __post_init__(self) -> None:
    if isinstance(self.side_effect_level, str):
      self.side_effect_level = SideEffectLevel(self.side_effect_level)
    if isinstance(self.execution_policy, dict):
      self.execution_policy = CapabilityExecutionPolicy(**self.execution_policy)
    if self.side_effect_level is SideEffectLevel.NONE:
      self.execution_policy.read_only = self.execution_policy.read_only or True
    if self.side_effect_level in {SideEffectLevel.WRITE, SideEffectLevel.EXEC, SideEffectLevel.EXTERNAL_MUTATION}:
      self.execution_policy.destructive = self.execution_policy.destructive or self.side_effect_level is not SideEffectLevel.WRITE

  @property
  def is_read_only(self) -> bool:
    return self.execution_policy.read_only or self.side_effect_level in {SideEffectLevel.NONE, SideEffectLevel.READ, SideEffectLevel.NETWORK}

  @property
  def is_concurrency_safe(self) -> bool:
    return self.execution_policy.concurrency_safe

  @property
  def requires_user_interaction(self) -> bool:
    return self.execution_policy.requires_user_interaction


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
  result_id: str = field(default_factory=lambda: new_id("tool_result"))
  capability_id: str | None = None
  tool_call_id: str | None = None
  provider: str | None = None
  status: Literal["succeeded", "failed", "cancelled", "killed"] | str | None = None
  started_at: datetime | None = None
  finished_at: datetime = field(default_factory=utc_now)
  metadata: dict[str, Any] = field(default_factory=dict)

  def __post_init__(self) -> None:
    if self.status is None:
      if self.ok:
        self.status = "succeeded"
      else:
        error_type = self.error.get("type") if self.error else None
        self.status = error_type if error_type in {"cancelled", "killed"} else "failed"

  @classmethod
  def success(
    cls,
    output: dict[str, Any] | None = None,
    *,
    artifact_refs: list[ArtifactRef] | None = None,
    events: list[RuntimeEvent] | None = None,
    metadata: dict[str, Any] | None = None,
  ) -> "ToolResult":
    return cls(
      ok=True,
      output=output or {},
      artifact_refs=artifact_refs or [],
      events=events or [],
      metadata=metadata or {},
      status="succeeded",
    )

  @classmethod
  def failure(
    cls,
    error_type: str,
    message: str | None = None,
    *,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    status: Literal["failed", "cancelled", "killed"] | str = "failed",
    metadata: dict[str, Any] | None = None,
  ) -> "ToolResult":
    envelope_error = dict(error or {})
    envelope_error.setdefault("type", error_type)
    if message is not None:
      envelope_error.setdefault("message", message)
    return cls(
      ok=False,
      output=output or {},
      error=envelope_error,
      status=status,
      metadata=metadata or {},
    )

  @classmethod
  def from_value(cls, value: Any) -> "ToolResult":
    if isinstance(value, ToolResult):
      return value
    if isinstance(value, dict):
      if "ok" in value:
        output = value.get("output", {})
        error = value.get("error")
        return cls(
          ok=bool(value["ok"]),
          output=output if isinstance(output, dict) else {"value": output},
          error=error if isinstance(error, dict) or error is None else {"type": "tool_error", "message": str(error)},
          metadata=value.get("metadata", {}) if isinstance(value.get("metadata"), dict) else {},
        )
      return cls.success(output=value)
    return cls.success(output={"value": value})

  def with_context(
    self,
    *,
    capability_id: str | None = None,
    tool_call_id: str | None = None,
    provider: str | None = None,
    metadata: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
  ) -> "ToolResult":
    merged_metadata = dict(self.metadata)
    if metadata:
      merged_metadata.update(metadata)
    return ToolResult(
      ok=self.ok,
      output=dict(self.output),
      error=dict(self.error) if self.error is not None else None,
      artifact_refs=list(self.artifact_refs),
      events=list(self.events),
      result_id=self.result_id,
      capability_id=capability_id or self.capability_id,
      tool_call_id=tool_call_id or self.tool_call_id,
      provider=provider or self.provider,
      status=self.status,
      started_at=started_at if started_at is not None else self.started_at,
      finished_at=finished_at if finished_at is not None else self.finished_at,
      metadata=merged_metadata,
    )

  def envelope(self) -> dict[str, Any]:
    return {
      "result_id": self.result_id,
      "ok": self.ok,
      "status": self.status,
      "capability_id": self.capability_id,
      "tool_call_id": self.tool_call_id,
      "provider": self.provider,
      "output": self.output,
      "error": self.error,
      "artifact_refs": [ref.to_dict() for ref in self.artifact_refs],
      "events": [event.to_dict() for event in self.events],
      "started_at": self.started_at.isoformat() if self.started_at is not None else None,
      "finished_at": self.finished_at.isoformat(),
      "metadata": self.metadata,
    }
