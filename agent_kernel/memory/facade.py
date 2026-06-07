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
from agent_kernel.memory.semantic import (
  FactConflict,
  FactConflictDetector,
  SemanticQuery,
  SemanticRetriever,
  SemanticSearchResult,
  SparseSemanticRetriever,
  StructuredFactConflictDetector,
)

MAX_INLINE_MEMORY_CHARS = 1200


class MemoryFacade:
  def __init__(
    self,
    uow_factory,
    episode_summarizer: EpisodeSummarizer | None = None,
    episode_retriever: EpisodeRetriever | None = None,
    semantic_retriever: SemanticRetriever | None = None,
    fact_conflict_detector: FactConflictDetector | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._episode_summarizer = episode_summarizer or DeterministicEpisodeSummarizer()
    self._episode_retriever = episode_retriever or KeywordEpisodeRetriever()
    self._semantic_retriever = semantic_retriever or SparseSemanticRetriever()
    self._fact_conflict_detector = fact_conflict_detector or StructuredFactConflictDetector()

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

  def write_semantic(
    self,
    scope: str,
    content: dict[str, Any],
    importance: float | None = None,
    confidence: float | None = None,
    created_by: str | None = None,
    check_conflicts: bool = False,
  ) -> MemoryItem:
    item = self.write(
      memory_type="semantic",
      scope=scope,
      content=content,
      importance=importance,
      confidence=confidence,
      created_by=created_by,
    )
    if check_conflicts:
      conflicts = self.detect_fact_conflicts(scope, item)
      if conflicts:
        item.content = {
          **item.content,
          "conflicts": [
            {
              "incoming_memory_id": conflict.incoming.memory_id,
              "existing_memory_id": conflict.existing.memory_id,
              "subject": conflict.incoming.subject,
              "predicate": conflict.incoming.predicate,
              "incoming_value": conflict.incoming.value,
              "existing_value": conflict.existing.value,
              "reason": conflict.reason,
              "severity": conflict.severity,
            }
            for conflict in conflicts
          ],
        }
        with self._uow_factory() as uow:
          uow.memory.save(item)
    return item

  def write(
    self,
    memory_type: str,
    scope: str,
    content: dict[str, Any],
    importance: float | None = None,
    confidence: float | None = None,
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
      confidence=confidence,
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

  def retrieve_semantic(
    self,
    scope: str,
    query: str,
    limit: int = 5,
    min_score: float = 0.0,
    memory_types: set[str] | None = None,
  ) -> list[SemanticSearchResult]:
    memories: list[MemoryItem] = []
    for memory_type in memory_types or {"semantic"}:
      memories.extend(self.retrieve(scope, memory_type=memory_type, limit=100))
    return self._semantic_retriever.retrieve(
      SemanticQuery(
        scope=scope,
        text=query,
        memory_types=memory_types or {"semantic"},
        limit=limit,
        min_score=min_score,
      ),
      memories,
    )

  def detect_fact_conflicts(
    self,
    scope: str,
    incoming: MemoryItem,
    memory_type: str | None = "semantic",
  ) -> list[FactConflict]:
    existing = [
      item
      for item in self.retrieve(scope, memory_type=memory_type, limit=200)
      if item.memory_id != incoming.memory_id
    ]
    return self._fact_conflict_detector.detect(incoming, existing)
