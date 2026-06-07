"""Skill and dynamic workflow patch domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef, MemoryRef
from agent_kernel.domain.workflow import ExecutionCommand


class SkillStatus(StrEnum):
  DRAFT = "draft"
  ACTIVE = "active"
  DEPRECATED = "deprecated"


class SkillExecutionMode(StrEnum):
  AGENT_INTERPRETED = "agent_interpreted"
  COMPILED_WORKFLOW = "compiled_workflow"


class PlanPatchStatus(StrEnum):
  PROPOSED = "proposed"
  ACCEPTED = "accepted"
  REJECTED = "rejected"
  APPLIED = "applied"


@dataclass(slots=True)
class SkillUsePolicy(DomainModel):
  allowed_agent_ids: list[str] = field(default_factory=list)
  allowed_workflow_ids: list[str] = field(default_factory=list)
  required_grants: list[str] = field(default_factory=list)
  max_risk_level: Literal["low", "medium", "high"] = "medium"
  require_human_approval: bool = False


@dataclass(slots=True)
class SkillCard(DomainModel):
  skill_id: str
  name: str
  description: str
  when_to_use: str
  instructions: str
  status: SkillStatus | str = SkillStatus.DRAFT
  execution_mode: SkillExecutionMode | str = SkillExecutionMode.AGENT_INTERPRETED
  inputs: dict[str, Any] = field(default_factory=dict)
  outputs: dict[str, Any] = field(default_factory=dict)
  recommended_tools: list[str] = field(default_factory=list)
  recommended_workflows: list[str] = field(default_factory=list)
  constraints: list[str] = field(default_factory=list)
  failure_modes: list[str] = field(default_factory=list)
  use_policy: SkillUsePolicy | None = None
  procedure_memory_ref: MemoryRef | None = None
  compiled_workflow_ref: str | None = None
  examples: list[ArtifactRef] = field(default_factory=list)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = SkillStatus(self.status)
    if isinstance(self.execution_mode, str):
      self.execution_mode = SkillExecutionMode(self.execution_mode)


@dataclass(slots=True)
class PlanPatch(DomainModel):
  patch_id: str
  run_id: str
  proposed_by_agent_id: str
  reason: str
  commands: list[ExecutionCommand]
  required_capabilities: list[str] = field(default_factory=list)
  expected_artifacts: list[str] = field(default_factory=list)
  risk_notes: list[str] = field(default_factory=list)
  status: PlanPatchStatus | str = PlanPatchStatus.PROPOSED
  created_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = PlanPatchStatus(self.status)

