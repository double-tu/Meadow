"""Episodic memory interfaces and default local implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_kernel.domain.memory import MemoryItem


@dataclass(slots=True)
class EpisodeInput:
  scope: str
  task_id: str | None
  event_ids: list[str]
  observations: list[str]
  outcome: str
  artifact_ids: list[str]


class EpisodeSummarizer(Protocol):
  def summarize(self, episode: EpisodeInput) -> dict[str, object]:
    ...


class EpisodeRetriever(Protocol):
  def retrieve(self, query: str, memories: list[MemoryItem], limit: int = 5) -> list[MemoryItem]:
    ...


class DeterministicEpisodeSummarizer:
  def summarize(self, episode: EpisodeInput) -> dict[str, object]:
    return {
      "task_id": episode.task_id,
      "summary": " ".join(episode.observations[:3]).strip(),
      "outcome": episode.outcome,
      "event_ids": episode.event_ids,
      "artifact_ids": episode.artifact_ids,
      "keywords": self._keywords([*episode.observations, episode.outcome]),
    }

  @staticmethod
  def _keywords(values: list[str]) -> list[str]:
    words: set[str] = set()
    for value in values:
      for raw in value.lower().replace("_", " ").split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if len(word) >= 3:
          words.add(word)
    return sorted(words)[:20]


class KeywordEpisodeRetriever:
  def retrieve(self, query: str, memories: list[MemoryItem], limit: int = 5) -> list[MemoryItem]:
    query_terms = set(DeterministicEpisodeSummarizer._keywords([query]))

    def score(memory: MemoryItem) -> tuple[float, str]:
      keywords = set(str(item) for item in memory.content.get("keywords", []))
      summary = str(memory.content.get("summary", "")).lower()
      overlap = len(query_terms & keywords)
      text_hits = sum(1 for term in query_terms if term in summary)
      importance = memory.importance or 0.0
      return (overlap * 2 + text_hits + importance, memory.created_at.isoformat())

    ranked = sorted(memories, key=score, reverse=True)
    return [memory for memory in ranked if score(memory)[0] > 0][:limit]
