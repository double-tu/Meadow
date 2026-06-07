"""Autonomous exploration and workflow induction domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef


class ExplorationStatus(StrEnum):
  CREATED = "created"
  PLANNING = "planning"
  RUNNING = "running"
  VERIFYING = "verifying"
  DISTILLING = "distilling"
  PUBLISHED = "published"
  FAILED = "failed"
  CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
  PLANNED = "planned"
  RUNNING = "running"
  SUCCEEDED = "succeeded"
  FAILED = "failed"
  CANCELLED = "cancelled"


class WorkflowTemplateStatus(StrEnum):
  DRAFT = "draft"
  VERIFIED = "verified"
  STABLE = "stable"
  DEPRECATED = "deprecated"


@dataclass(slots=True)
class AcceptanceCriteria(DomainModel):
  criteria_id: str
  description: str
  verifier_ref: str | None = None
  required: bool = True


@dataclass(slots=True)
class ExplorationTask(DomainModel):
  exploration_id: str
  objective_id: str
  status: ExplorationStatus | str
  problem_statement: str
  task_id: str | None = None
  acceptance_criteria: list[AcceptanceCriteria] = field(default_factory=list)
  max_attempts: int = 5
  risk_budget: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = ExplorationStatus(self.status)


@dataclass(slots=True)
class CandidateStrategy(DomainModel):
  strategy_id: str
  exploration_id: str
  hypothesis: str
  tool_refs: list[str] = field(default_factory=list)
  workflow_refs: list[str] = field(default_factory=list)
  agent_refs: list[str] = field(default_factory=list)
  expected_artifacts: list[str] = field(default_factory=list)
  risk_notes: list[str] = field(default_factory=list)
  priority: int = 0


@dataclass(slots=True)
class ExplorationAttempt(DomainModel):
  attempt_id: str
  exploration_id: str
  strategy_id: str
  run_id: str
  status: AttemptStatus | str
  result_summary: str | None = None
  failure_reason: str | None = None
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  event_refs: list[str] = field(default_factory=list)
  started_at: datetime | None = None
  completed_at: datetime | None = None

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = AttemptStatus(self.status)


@dataclass(slots=True)
class GoldenTrace(DomainModel):
  trace_id: str
  source_run_id: str
  source_attempt_id: str
  event_refs: list[str]
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  verified_by: str | None = None
  created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkflowTemplate(DomainModel):
  template_id: str
  name: str
  status: WorkflowTemplateStatus | str
  workflow_spec_ref: str
  applicability: str
  source_trace_id: str | None = None
  limitations: list[str] = field(default_factory=list)
  cost_profile: dict[str, Any] = field(default_factory=dict)
  risk_profile: dict[str, Any] = field(default_factory=dict)
  eval_suite_ref: str | None = None
  version: str = "0.1.0"

  def __post_init__(self) -> None:
    if isinstance(self.status, str):
      self.status = WorkflowTemplateStatus(self.status)


@dataclass(slots=True)
class SkillEvolutionRecord(DomainModel):
  record_id: str
  source_trace_id: str
  decision: Literal["create_skill", "update_skill", "compile_workflow", "reject"]
  rationale: str
  skill_id: str | None = None
  workflow_template_id: str | None = None
  created_at: datetime = field(default_factory=utc_now)

