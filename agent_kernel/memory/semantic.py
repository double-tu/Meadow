"""Semantic memory retrieval and conflict detection interfaces."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import math
import re
from typing import Any, Protocol

from agent_kernel.domain.memory import MemoryItem


TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_]+")


@dataclass(slots=True)
class SemanticQuery:
  scope: str
  text: str
  memory_types: set[str] = field(default_factory=lambda: {"semantic"})
  limit: int = 5
  min_score: float = 0.0


@dataclass(slots=True)
class SemanticSearchResult:
  memory: MemoryItem
  score: float
  rationale: str


class SemanticRetriever(Protocol):
  def retrieve(self, query: SemanticQuery, memories: list[MemoryItem]) -> list[SemanticSearchResult]:
    """Rank candidate memories for a semantic query."""


@dataclass(slots=True)
class FactStatement:
  subject: str
  predicate: str
  value: str
  memory_id: str | None = None
  confidence: float | None = None

  @property
  def key(self) -> tuple[str, str]:
    return (_normalize_fact_text(self.subject), _normalize_fact_text(self.predicate))


@dataclass(slots=True)
class FactConflict:
  incoming: FactStatement
  existing: FactStatement
  reason: str
  severity: str = "warning"


class FactConflictDetector(Protocol):
  def detect(self, incoming: MemoryItem, existing: list[MemoryItem]) -> list[FactConflict]:
    """Detect contradictions between one memory and existing memories."""


class SparseSemanticRetriever:
  """Dependency-free sparse semantic retriever.

  This is intentionally simple and deterministic. It gives Meadow a real local
  semantic retrieval path now, while keeping the interface replaceable by an
  embedding/vector-store implementation later.
  """

  def retrieve(self, query: SemanticQuery, memories: list[MemoryItem]) -> list[SemanticSearchResult]:
    query_vector = _text_vector(query.text)
    results: list[SemanticSearchResult] = []
    for memory in memories:
      if memory.memory_type not in query.memory_types:
        continue
      memory_vector = _memory_vector(memory)
      score = _cosine_similarity(query_vector, memory_vector)
      if score < query.min_score:
        continue
      results.append(
        SemanticSearchResult(
          memory=memory,
          score=score,
          rationale=_semantic_rationale(query_vector, memory_vector, memory),
        )
      )
    results.sort(
      key=lambda result: (
        result.score,
        result.memory.importance or 0.0,
        result.memory.created_at.isoformat(),
      ),
      reverse=True,
    )
    return results[: query.limit]


class StructuredFactConflictDetector:
  """Detects conflicts for structured fact-like memory content."""

  def detect(self, incoming: MemoryItem, existing: list[MemoryItem]) -> list[FactConflict]:
    incoming_facts = extract_fact_statements(incoming)
    conflicts: list[FactConflict] = []
    for existing_memory in existing:
      for incoming_fact in incoming_facts:
        for existing_fact in extract_fact_statements(existing_memory):
          if incoming_fact.key != existing_fact.key:
            continue
          if _normalize_fact_text(incoming_fact.value) == _normalize_fact_text(existing_fact.value):
            continue
          conflicts.append(
            FactConflict(
              incoming=incoming_fact,
              existing=existing_fact,
              reason=(
                f"Conflicting values for {incoming_fact.subject}.{incoming_fact.predicate}: "
                f"{incoming_fact.value!r} != {existing_fact.value!r}."
              ),
              severity="error" if _is_high_confidence_pair(incoming_fact, existing_fact) else "warning",
            )
          )
    return conflicts


def extract_fact_statements(memory: MemoryItem) -> list[FactStatement]:
  """Extract normalized fact statements from a memory item."""

  content = memory.content
  raw_facts = content.get("facts")
  facts: list[FactStatement] = []
  if isinstance(raw_facts, list):
    for item in raw_facts:
      fact = _fact_from_mapping(item, memory)
      if fact is not None:
        facts.append(fact)
  fact = _fact_from_mapping(content, memory)
  if fact is not None:
    facts.append(fact)
  return facts


def _fact_from_mapping(value: object, memory: MemoryItem) -> FactStatement | None:
  if not isinstance(value, dict):
    return None
  subject = value.get("subject") or value.get("entity") or value.get("key")
  predicate = value.get("predicate") or value.get("attribute") or value.get("field")
  fact_value = value.get("value")
  if not isinstance(subject, str) or not isinstance(predicate, str):
    return None
  if fact_value is None:
    return None
  return FactStatement(
    subject=subject,
    predicate=predicate,
    value=str(fact_value),
    memory_id=memory.memory_id,
    confidence=memory.confidence,
  )


def _memory_vector(memory: MemoryItem) -> Counter[str]:
  values = [_flatten_text(memory.content)]
  keywords = memory.content.get("keywords")
  if isinstance(keywords, list):
    values.extend(str(keyword) for keyword in keywords)
  return _text_vector(" ".join(values))


def _text_vector(text: str) -> Counter[str]:
  return Counter(_tokens(text))


def _tokens(text: str) -> list[str]:
  return [
    token
    for token in (match.group(0).lower() for match in TOKEN_PATTERN.finditer(text.replace("_", " ")))
    if len(token) >= 2
  ]


def _flatten_text(value: Any) -> str:
  if isinstance(value, dict):
    return " ".join(f"{key} {_flatten_text(item)}" for key, item in value.items())
  if isinstance(value, list):
    return " ".join(_flatten_text(item) for item in value)
  return str(value)


def _cosine_similarity(left: Counter[str], right: Counter[str]) -> float:
  if not left or not right:
    return 0.0
  dot = sum(count * right.get(term, 0) for term, count in left.items())
  left_norm = math.sqrt(sum(count * count for count in left.values()))
  right_norm = math.sqrt(sum(count * count for count in right.values()))
  if left_norm == 0.0 or right_norm == 0.0:
    return 0.0
  return dot / (left_norm * right_norm)


def _semantic_rationale(
  query_vector: Counter[str],
  memory_vector: Counter[str],
  memory: MemoryItem,
) -> str:
  overlaps = sorted(set(query_vector) & set(memory_vector))
  if overlaps:
    return f"Matched terms {', '.join(overlaps[:8])} in {memory.memory_type} memory."
  return f"Selected by importance/recency from {memory.memory_type} memory."


def _normalize_fact_text(value: str) -> str:
  return " ".join(_tokens(value))


def _is_high_confidence_pair(left: FactStatement, right: FactStatement) -> bool:
  return (left.confidence or 0.0) >= 0.8 and (right.confidence or 0.0) >= 0.8
