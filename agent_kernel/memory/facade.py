"""Memory facade."""

from typing import Any

from agent_kernel.domain.base import new_id
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.memory import MemoryItem

MAX_INLINE_MEMORY_CHARS = 1200


class MemoryFacade:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def write_working(
    self,
    scope: str,
    content: dict[str, Any],
    importance: float | None = None,
    created_by: str | None = None,
  ) -> MemoryItem:
    return self.write(
      memory_type="working",
      scope=scope,
      content=content,
      importance=importance,
      created_by=created_by,
    )

  def write_artifact_memory(
    self,
    scope: str,
    artifact: ArtifactRef,
    content: dict[str, Any] | None = None,
    importance: float | None = None,
  ) -> MemoryItem:
    return self.write(
      memory_type="artifact",
      scope=scope,
      content=content or {"artifact_id": artifact.artifact_id, "uri": artifact.uri},
      source_artifact_refs=[artifact],
      importance=importance,
    )

  def write(
    self,
    memory_type: str,
    scope: str,
    content: dict[str, Any],
    importance: float | None = None,
    created_by: str | None = None,
    source_artifact_refs: list[ArtifactRef] | None = None,
  ) -> MemoryItem:
    artifact_refs = source_artifact_refs or []
    normalized_content = content
    if len(str(content)) > MAX_INLINE_MEMORY_CHARS:
      artifact = ArtifactRef(
        artifact_id=new_id("art"),
        uri=f"memory://{scope}/{new_id('large_memory')}",
        media_type="application/json",
      )
      artifact_refs = [*artifact_refs, artifact]
      normalized_content = {
        "summary": "Large memory content stored as artifact reference.",
        "artifact_id": artifact.artifact_id,
        "uri": artifact.uri,
      }
    item = MemoryItem(
      memory_id=new_id("mem"),
      memory_type=memory_type,  # type: ignore[arg-type]
      scope=scope,
      content=normalized_content,
      importance=importance,
      created_by=created_by,
      source_artifact_refs=artifact_refs,
    )
    with self._uow_factory() as uow:
      uow.memory.save(item)
    return item

  def retrieve(self, scope: str, memory_type: str | None = None, limit: int = 20) -> list[MemoryItem]:
    with self._uow_factory() as uow:
      return uow.memory.list_by_scope(scope, memory_type=memory_type, limit=limit)
