"""Semantic memory retrieval and conflict detection interfaces."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import math
import re
from typing import Any, Protocol
from urllib import error as url_error
from urllib import request as url_request

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
class VectorStoreDocument:
  document_id: str
  scope: str
  text: str
  metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VectorStoreMatch:
  document_id: str
  score: float
  metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
  def upsert(self, documents: list[VectorStoreDocument]) -> None:
    ...

  def query(
    self,
    scope: str,
    text: str,
    limit: int,
    min_score: float = 0.0,
    filters: dict[str, Any] | None = None,
  ) -> list[VectorStoreMatch]:
    ...


@dataclass(slots=True)
class HTTPVectorStoreEndpoint:
  base_url: str
  upsert_path: str = "/upsert"
  query_path: str = "/query"
  timeout_seconds: float = 30.0
  headers: dict[str, str] = field(default_factory=dict)

  def url(self, path: str) -> str:
    return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


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


class InMemoryVectorStore:
  """Deterministic vector-store adapter for tests and local fallback."""

  def __init__(self) -> None:
    self._documents: dict[str, VectorStoreDocument] = {}

  def upsert(self, documents: list[VectorStoreDocument]) -> None:
    for document in documents:
      self._documents[document.document_id] = document

  def query(
    self,
    scope: str,
    text: str,
    limit: int,
    min_score: float = 0.0,
    filters: dict[str, Any] | None = None,
  ) -> list[VectorStoreMatch]:
    query_vector = _text_vector(text)
    matches: list[VectorStoreMatch] = []
    filters = filters or {}
    for document in self._documents.values():
      if document.scope != scope:
        continue
      if any(document.metadata.get(key) != value for key, value in filters.items()):
        continue
      score = _cosine_similarity(query_vector, _text_vector(document.text))
      if score < min_score:
        continue
      matches.append(VectorStoreMatch(document_id=document.document_id, score=score, metadata=dict(document.metadata)))
    matches.sort(key=lambda match: (match.score, match.document_id), reverse=True)
    return matches[:limit]


class HTTPVectorStore:
  """HTTP JSON adapter for remote vector-store services."""

  def __init__(
    self,
    endpoint: HTTPVectorStoreEndpoint | str,
    transport: Any | None = None,
  ) -> None:
    self._endpoint = endpoint if isinstance(endpoint, HTTPVectorStoreEndpoint) else HTTPVectorStoreEndpoint(endpoint)
    self._transport = transport or self._post_json

  def upsert(self, documents: list[VectorStoreDocument]) -> None:
    payload = {
      "documents": [
        {
          "document_id": document.document_id,
          "scope": document.scope,
          "text": document.text,
          "metadata": document.metadata,
        }
        for document in documents
      ]
    }
    self._transport(self._endpoint.url(self._endpoint.upsert_path), payload, self._endpoint)

  def query(
    self,
    scope: str,
    text: str,
    limit: int,
    min_score: float = 0.0,
    filters: dict[str, Any] | None = None,
  ) -> list[VectorStoreMatch]:
    payload = {
      "scope": scope,
      "text": text,
      "limit": limit,
      "min_score": min_score,
      "filters": filters or {},
    }
    response = self._transport(self._endpoint.url(self._endpoint.query_path), payload, self._endpoint)
    return [self._match_from_value(value) for value in self._extract_matches(response)]

  @staticmethod
  def _post_json(
    url: str,
    payload: dict[str, Any],
    endpoint: HTTPVectorStoreEndpoint,
  ) -> dict[str, Any] | list[Any]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    request = url_request.Request(
      url,
      data=body,
      headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        **endpoint.headers,
      },
      method="POST",
    )
    try:
      with url_request.urlopen(request, timeout=endpoint.timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    except url_error.HTTPError as exc:
      raise RuntimeError(f"Vector store HTTP error {exc.code}: {exc.reason}") from exc
    except url_error.URLError as exc:
      raise RuntimeError(f"Vector store connection failed: {exc.reason}") from exc
    except TimeoutError as exc:
      raise RuntimeError("Vector store request timed out.") from exc
    try:
      parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
      raise RuntimeError("Vector store returned invalid JSON.") from exc
    if not isinstance(parsed, (dict, list)):
      raise RuntimeError("Vector store response must be a JSON object or array.")
    return parsed

  @staticmethod
  def _extract_matches(response: dict[str, Any] | list[Any]) -> list[Any]:
    if isinstance(response, list):
      return response
    for key in ("matches", "results", "items", "documents"):
      value = response.get(key)
      if isinstance(value, list):
        return value
    return []

  @staticmethod
  def _match_from_value(value: Any) -> VectorStoreMatch:
    if not isinstance(value, dict):
      raise RuntimeError("Vector store match must be a JSON object.")
    document_id = value.get("document_id") or value.get("id") or value.get("memory_id")
    if not isinstance(document_id, str):
      raise RuntimeError("Vector store match requires document_id.")
    score = value.get("score", 0.0)
    metadata = value.get("metadata", {})
    return VectorStoreMatch(
      document_id=document_id,
      score=float(score),
      metadata=metadata if isinstance(metadata, dict) else {},
    )


class VectorStoreSemanticRetriever:
  """Semantic retriever backed by an injectable vector store.

  The retriever indexes the provided candidate memories before querying the
  store, so callers can keep using the existing SemanticRetriever interface.
  Production hosts can replace InMemoryVectorStore with a remote vector DB
  adapter without changing MemoryFacade or context code.
  """

  def __init__(self, vector_store: VectorStore) -> None:
    self._vector_store = vector_store

  def retrieve(self, query: SemanticQuery, memories: list[MemoryItem]) -> list[SemanticSearchResult]:
    candidates = [memory for memory in memories if memory.memory_type in query.memory_types]
    self._vector_store.upsert([_memory_document(memory) for memory in candidates])
    by_id = {memory.memory_id: memory for memory in candidates}
    matches = self._vector_store.query(
      scope=query.scope,
      text=query.text,
      limit=query.limit,
      min_score=query.min_score,
      filters={"memory_type": next(iter(query.memory_types))} if len(query.memory_types) == 1 else None,
    )
    results: list[SemanticSearchResult] = []
    for match in matches:
      memory = by_id.get(match.document_id)
      if memory is None:
        continue
      results.append(
        SemanticSearchResult(
          memory=memory,
          score=match.score,
          rationale=f"Matched by vector store document {match.document_id}.",
        )
      )
    return results


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


def _memory_document(memory: MemoryItem) -> VectorStoreDocument:
  return VectorStoreDocument(
    document_id=memory.memory_id,
    scope=memory.scope,
    text=_flatten_text(memory.content),
    metadata={
      "memory_id": memory.memory_id,
      "memory_type": memory.memory_type,
      "importance": memory.importance,
      "created_by": memory.created_by,
    },
  )


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
