"""Multi-agent interaction domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef


class ParticipantKind(StrEnum):
  AGENT = "agent"
  HUMAN = "human"
  REMOTE_AGENT = "remote_agent"
  OBSERVER = "observer"


class ChannelMode(StrEnum):
  ROUND_ROBIN = "round_robin"
  AD_HOC = "ad_hoc"
  REQUEST_RESPONSE = "request_response"
  BROADCAST = "broadcast"
  REVIEW_QUEUE = "review_queue"
  OBSERVER_FEEDBACK = "observer_feedback"


class SpeakerPolicyType(StrEnum):
  ROUND_ROBIN = "round_robin"
  FIXED_ORDER = "fixed_order"
  MODERATOR_SELECT = "moderator_select"
  FREE_FOR_ALL = "free_for_all"
  EVENT_DRIVEN = "event_driven"


@dataclass(slots=True)
class InteractionParticipant(DomainModel):
  participant_id: str
  kind: ParticipantKind | str
  role: str
  agent_session_id: str | None = None
  human_user_id: str | None = None
  permissions: list[str] = field(default_factory=list)

  def __post_init__(self) -> None:
    if isinstance(self.kind, str):
      self.kind = ParticipantKind(self.kind)


@dataclass(slots=True)
class InteractionChannel(DomainModel):
  channel_id: str
  topic: str
  mode: ChannelMode | str
  participant_ids: list[str]
  bound_objective_id: str | None = None
  bound_task_id: str | None = None
  bound_run_id: str | None = None
  bound_artifact_refs: list[ArtifactRef] = field(default_factory=list)
  retention_policy: str | None = None
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if isinstance(self.mode, str):
      self.mode = ChannelMode(self.mode)


@dataclass(slots=True)
class InteractionMessage(DomainModel):
  message_id: str
  channel_id: str
  sender_participant_id: str
  content: dict[str, Any]
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  causal_event_id: str | None = None
  created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class SpeakerPolicy(DomainModel):
  type: SpeakerPolicyType | str
  fixed_order: list[str] = field(default_factory=list)
  max_turns_per_participant: int | None = None
  allow_user_interrupt: bool = True
  require_moderator_approval: bool = False

  def __post_init__(self) -> None:
    if isinstance(self.type, str):
      self.type = SpeakerPolicyType(self.type)


@dataclass(slots=True)
class GroupChatSession(DomainModel):
  group_chat_id: str
  thread_id: str
  topic: str
  participant_ids: list[str]
  speaker_policy: SpeakerPolicy
  objective_id: str | None = None
  moderator_participant_id: str | None = None
  turn_limit: int = 10
  consensus_rule: str | None = None
  decision_artifact_ref: ArtifactRef | None = None
  status: Literal["created", "running", "paused", "completed", "cancelled"] = "created"
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class DiscussionTurn(DomainModel):
  turn_id: str
  group_chat_id: str
  speaker_participant_id: str
  message_id: str
  turn_index: int
  selected_by: str | None = None
  rationale: str | None = None
  created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AgentPool(DomainModel):
  pool_id: str
  name: str
  role: str
  agent_session_ids: list[str] = field(default_factory=list)
  capability_tags: list[str] = field(default_factory=list)
  selection_policy: Literal["least_loaded", "capability_match", "round_robin", "manual", "success_rate"] = "capability_match"


@dataclass(slots=True)
class TaskBoardItem(DomainModel):
  item_id: str
  title: str
  status: Literal["todo", "doing", "review", "done", "blocked"] = "todo"
  assignee_pool_id: str | None = None
  assignee_session_id: str | None = None
  result_artifact_refs: list[ArtifactRef] = field(default_factory=list)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ObservationFinding(DomainModel):
  finding_id: str
  observer_id: str
  target_run_id: str
  severity: Literal["info", "warning", "critical"]
  action: Literal["comment", "warn", "intervene", "propose_patch", "request_pause", "request_cancel"]
  message: str
  evidence_event_refs: list[str] = field(default_factory=list)
  evidence_artifact_refs: list[ArtifactRef] = field(default_factory=list)
  created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class WorkspaceLease(DomainModel):
  lease_id: str
  task_id: str
  agent_session_id: str
  workspace_uri: str
  base_ref: str | None = None
  isolation_mode: Literal["worktree", "copy", "container", "remote"] = "worktree"
  status: Literal["active", "released", "abandoned"] = "active"
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class PatchArtifact(DomainModel):
  patch_id: str
  lease_id: str
  task_id: str
  author_session_id: str
  artifact_ref: ArtifactRef
  summary: str
  status: Literal["draft", "submitted", "approved", "rejected", "merged"] = "draft"
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class ReviewRecord(DomainModel):
  review_id: str
  patch_id: str
  reviewer_id: str
  decision: Literal["approved", "changes_requested", "rejected"]
  comments: list[str] = field(default_factory=list)
  created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class HandoffRecord(DomainModel):
  handoff_id: str
  task_id: str
  from_participant_id: str
  to_participant_id: str
  reason: str
  state_summary: str
  expected_output: str
  source_channel_id: str | None = None
  target_channel_id: str | None = None
  constraints: list[str] = field(default_factory=list)
  acceptance_criteria: list[str] = field(default_factory=list)
  artifact_refs: list[ArtifactRef] = field(default_factory=list)
  status: Literal["requested", "accepted", "rejected", "cancelled"] = "requested"
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)
