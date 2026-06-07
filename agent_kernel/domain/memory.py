"""Memory domain models."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, utc_now
from agent_kernel.domain.identifiers import ArtifactRef


@dataclass(slots=True)
class MemoryItem(DomainModel):
  memory_id: str
  memory_type: Literal["working", "episodic", "semantic", "procedural", "artifact"]
  scope: str
  content: dict[str, Any]
  source_event_ids: list[str] = field(default_factory=list)
  source_artifact_refs: list[ArtifactRef] = field(default_factory=list)
  confidence: float | None = None
  importance: float | None = None
  sensitivity: Literal["public", "internal", "confidential", "secret"] = "internal"
  ttl_seconds: int | None = None
  version: str = "1"
  created_by: str | None = None
  verified_by: str | None = None
  last_used_at: datetime | None = None
  created_at: datetime = field(default_factory=utc_now)

