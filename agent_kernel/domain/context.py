"""Context building domain models."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel
from agent_kernel.domain.identifiers import ArtifactRef, MemoryRef


class ContextLayerKind(StrEnum):
  SYSTEM_POLICY = "system_policy"
  AGENT_PROFILE = "agent_profile"
  SKILL_TOOL_INDEX = "skill_tool_index"
  WORKING_MEMORY = "working_memory"
  CONVERSATION_WINDOW = "conversation_window"
  EPISODIC_ARTIFACT = "episodic_artifact"
  LONG_TERM_MEMORY = "long_term_memory"


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


@dataclass(slots=True)
class ContextAssemblyRequest(DomainModel):
  run_id: str
  scope: str
  model_ref: str
  messages: list[dict[str, Any]]
  agent_id: str | None = None
  task_id: str | None = None
  system_instructions: str | None = None
  agent_profile: dict[str, Any] = field(default_factory=dict)
  skills: list[Any] = field(default_factory=list)
  tool_schemas: list[dict[str, Any]] = field(default_factory=list)
  max_tokens: int = 4096
  allowed_sensitivities: set[str] = field(default_factory=lambda: {"public", "internal"})
  layer_weights: dict[str, float] = field(default_factory=dict)
  metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ContextLayerItem(DomainModel):
  item_id: str
  layer: ContextLayerKind | str
  role: str
  content: Any
  token_estimate: int
  priority: float
  source_type: str
  source_ref: str | None = None
  memory_ref: MemoryRef | None = None
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  sensitivity: Literal["public", "internal", "confidential", "secret"] = "internal"
  rationale: str | None = None

  def __post_init__(self) -> None:
    if isinstance(self.layer, str):
      self.layer = ContextLayerKind(self.layer)


@dataclass(slots=True)
class ContextLayer(DomainModel):
  kind: ContextLayerKind | str
  budget_tokens: int
  items: list[ContextLayerItem] = field(default_factory=list)
  omitted_items: list[ContextLayerItem] = field(default_factory=list)
  token_estimate: int = 0
  rationale: str | None = None

  def __post_init__(self) -> None:
    if isinstance(self.kind, str):
      self.kind = ContextLayerKind(self.kind)


@dataclass(slots=True)
class LayerBudget(DomainModel):
  max_tokens: int
  layer_budgets: dict[str, int]


@dataclass(slots=True)
class ContextPack(DomainModel):
  pack_id: str
  run_id: str
  model_ref: str
  layers: list[ContextLayer]
  model_context: ModelContext
  omitted_items: list[ContextLayerItem] = field(default_factory=list)
  quality_warnings: list[str] = field(default_factory=list)
  token_budget_ledger: dict[str, int] = field(default_factory=dict)
