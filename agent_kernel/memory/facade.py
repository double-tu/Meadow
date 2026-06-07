"""Memory facade."""

from typing import Any

from agent_kernel.domain.base import new_id
from agent_kernel.domain.identifiers import ArtifactRef
from agent_kernel.domain.memory import MemoryItem
from agent_kernel.memory.episodic import (
  DeterministicEpisodeSummarizer,
  EpisodeInput,
  EpisodeRetriever,
  EpisodeSummarizer,
  KeywordEpisodeRetriever,
)

MAX_INLINE_MEMORY_CHARS = 1200


class MemoryFacade:
  def __init__(
    self,
    uow_factory,
    episode_summarizer: EpisodeSummarizer | None = None,
    episode_retriever: EpisodeRetriever | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._episode_summarizer = episode_summarizer or DeterministicEpisodeSummarizer()
    self._episode_retriever = episode_retriever or KeywordEpisodeRetriever()

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

  def write_episode(
    self,
    scope: str,
    task_id: str | None,
    event_ids: list[str],
    observations: list[str],
    outcome: str,
    artifact_refs: list[ArtifactRef] | None = None,
    importance: float | None = None,
    created_by: str | None = None,
  ) -> MemoryItem:
    artifact_refs = artifact_refs or []
    episode = EpisodeInput(
      scope=scope,
      task_id=task_id,
      event_ids=event_ids,
      observations=observations,
      outcome=outcome,
      artifact_ids=[ref.artifact_id for ref in artifact_refs],
    )
    return self.write(
      memory_type="episodic",
      scope=scope,
      content=self._episode_summarizer.summarize(episode),
      importance=importance,
      created_by=created_by,
      source_artifact_refs=artifact_refs,
      source_event_ids=event_ids,
    )

  def consolidate_episode(
    self,
    scope: str,
    episode: MemoryItem,
    importance_threshold: float = 0.8,
  ) -> MemoryItem | None:
    if episode.memory_type != "episodic":
      raise ValueError("Only episodic memories can be consolidated.")
    if (episode.importance or 0.0) < importance_threshold:
      return None
    return self.write(
      memory_type="semantic",
      scope=scope,
      content={
        "summary": episode.content.get("summary"),
        "source_episode_id": episode.memory_id,
        "keywords": episode.content.get("keywords", []),
        "outcome": episode.content.get("outcome"),
      },
      importance=episode.importance,
      created_by="episodic_consolidation",
      source_artifact_refs=episode.source_artifact_refs,
      source_event_ids=episode.source_event_ids,
    )

  def write(
    self,
    memory_type: str,
    scope: str,
    content: dict[str, Any],
    importance: float | None = None,
    created_by: str | None = None,
    source_artifact_refs: list[ArtifactRef] | None = None,
    source_event_ids: list[str] | None = None,
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
      source_event_ids=source_event_ids or [],
    )
    with self._uow_factory() as uow:
      uow.memory.save(item)
    return item

  def retrieve(self, scope: str, memory_type: str | None = None, limit: int = 20) -> list[MemoryItem]:
    with self._uow_factory() as uow:
      return uow.memory.list_by_scope(scope, memory_type=memory_type, limit=limit)

  def retrieve_episodic(self, scope: str, query: str, limit: int = 5) -> list[MemoryItem]:
    memories = self.retrieve(scope, memory_type="episodic", limit=100)
    return self._episode_retriever.retrieve(query, memories, limit=limit)
