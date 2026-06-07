"""Identifier and reference value objects."""

from dataclasses import dataclass

from agent_kernel.domain.base import DomainModel


@dataclass(slots=True)
class EntityRef(DomainModel):
  id: str
  kind: str


@dataclass(slots=True)
class ArtifactRef(DomainModel):
  artifact_id: str
  uri: str
  media_type: str | None = None
  version: str | None = None
  checksum: str | None = None


@dataclass(slots=True)
class MemoryRef(DomainModel):
  memory_id: str
  memory_type: str
  score: float | None = None

