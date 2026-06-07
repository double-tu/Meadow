"""Context building domain models."""

from dataclasses import dataclass, field
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel
from agent_kernel.domain.identifiers import ArtifactRef, MemoryRef


@dataclass(slots=True)
class RetrievalPack(DomainModel):
  working_snapshot: dict[str, Any] = field(default_factory=dict)
  episodic_refs: list[MemoryRef] = field(default_factory=list)
  semantic_refs: list[MemoryRef] = field(default_factory=list)
  procedural_refs: list[MemoryRef] = field(default_factory=list)
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  confidence_score: float | None = None
  sensitivity_marks: list[str] = field(default_factory=list)
  token_estimate: int | None = None


@dataclass(slots=True)
class ModelContext(DomainModel):
  messages: list[dict[str, Any]]
  tool_schemas: list[dict[str, Any]] = field(default_factory=list)
  attachments: list[ArtifactRef] = field(default_factory=list)
  memory_refs: list[MemoryRef] = field(default_factory=list)
  omitted_candidates: list[MemoryRef] = field(default_factory=list)
  token_budget_ledger: dict[str, int] = field(default_factory=dict)
  inclusion_rationale: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ContextCandidate(DomainModel):
  candidate_id: str
  source_type: Literal[
    "message",
    "memory",
    "artifact",
    "tool_result",
    "workflow_state",
    "schema",
    "instruction",
  ]
  source_ref: str
  token_estimate: int
  relevance_score: float
  freshness_score: float | None = None
  sensitivity: Literal["public", "internal", "confidential", "secret"] = "internal"
  include: bool = False
  rationale: str | None = None


@dataclass(slots=True)
class ContextPlan(DomainModel):
  plan_id: str
  run_id: str
  model_ref: str
  partition_budgets: dict[str, int]
  candidates: list[ContextCandidate] = field(default_factory=list)
  selected_candidate_ids: list[str] = field(default_factory=list)
  omitted_candidate_ids: list[str] = field(default_factory=list)
  tool_visibility: list[str] = field(default_factory=list)
  compression_strategy: str | None = None
  density_score: float | None = None
  quality_warnings: list[str] = field(default_factory=list)

