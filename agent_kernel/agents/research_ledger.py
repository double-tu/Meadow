"""Model-visible research ledger for exploratory agent runs.

The ledger is deliberately passive: it summarizes evidence, candidate sources,
failures, and pressure to change strategy. It does not decide which site to
open or which workflow to run; Skills/SOPs and the model still own procedure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Protocol


class ToolOutcomeView(Protocol):
  name: str
  capability_id: str
  input: dict[str, Any]
  ok: bool
  output: dict[str, Any]
  error: dict[str, Any] | None


@dataclass(slots=True)
class ResearchLedger:
  """Compact state for research-like tasks and browser exploration."""

  original_goal: str
  enabled: bool = False
  visited_sources: list[dict[str, Any]] = field(default_factory=list)
  evidence: list[dict[str, Any]] = field(default_factory=list)
  candidate_sources: list[dict[str, Any]] = field(default_factory=list)
  failures: list[dict[str, Any]] = field(default_factory=list)
  strategy_notes: list[str] = field(default_factory=list)
  max_items: int = 24

  @classmethod
  def from_goal(cls, goal: str) -> "ResearchLedger":
    return cls(original_goal=goal, enabled=_looks_like_research_goal(goal))

  def observe_records(self, records: list[ToolOutcomeView], *, turn: int) -> None:
    if any(_record_is_research_related(record) for record in records):
      self.enabled = True
    if not self.enabled:
      return
    for record in records:
      self._observe_record(record, turn=turn)
    self._trim()

  def render_message(self, *, current_turn: int) -> dict[str, Any] | None:
    if not self.enabled:
      return None
    content = {
      "type": "research_ledger",
      "original_goal": self.original_goal,
      "current_turn": current_turn,
      "visited_sources": self.visited_sources[-10:],
      "evidence": self.evidence[-10:],
      "candidate_sources": self.candidate_sources[-12:],
      "failures": self.failures[-8:],
      "strategy_notes": self.strategy_notes[-8:],
      "instructions": [
        "Use this ledger to continue the same investigation without restarting or repeating low-value scans.",
        "Search/result pages are candidate discovery, not enough by themselves; open promising candidate sources and extract evidence before finalizing.",
        "When a browser scan repeats or gives low evidence, switch to precise browser_execute_js extraction, a different source/query, HTTP, Skill/SOP, delegation, or a concrete blocker report.",
        "Final answers for research tasks should separate verified evidence, uncertainty, and next recoverable steps when incomplete.",
      ],
    }
    return {"role": "system", "content": content}

  def to_model_payload(self) -> dict[str, Any] | None:
    if not self.enabled:
      return None
    return {
      "type": "research_ledger",
      "visited_sources": self.visited_sources[-8:],
      "evidence": self.evidence[-8:],
      "candidate_sources": self.candidate_sources[-10:],
      "failures": self.failures[-6:],
      "strategy_notes": self.strategy_notes[-6:],
    }

  def _observe_record(self, record: ToolOutcomeView, *, turn: int) -> None:
    if not record.ok:
      self.failures.append(
        {
          "turn": turn,
          "tool": record.name,
          "capability_id": record.capability_id,
          "error_type": (record.error or {}).get("type", "unknown_error"),
          "message": _compact(str((record.error or {}).get("message") or ""), 260),
        }
      )
      return
    output = record.output
    url = _record_url(output, record.input)
    if url:
      self._append_unique(
        self.visited_sources,
        {"turn": turn, "tool": record.name, "url": url, "title": _record_title(output)},
        keys=("url",),
      )
    page = output.get("page")
    if isinstance(page, dict):
      self._observe_page(record, page, turn=turn)
      return
    self._observe_non_page_output(record, turn=turn)

  def _observe_page(self, record: ToolOutcomeView, page: dict[str, Any], *, turn: int) -> None:
    title = _string(page.get("title"))
    url = _string(page.get("url")) or _record_url(record.output, record.input)
    text = _string(page.get("text")) or ""
    search_results = _dict_list(page.get("search_results"))
    links = _dict_list(page.get("links"))
    feed_titles = _string_list(page.get("feed_titles"))
    visible_cards = _string_list(page.get("visible_cards"))
    if url:
      self._append_unique(
        self.visited_sources,
        {"turn": turn, "tool": record.name, "url": url, "title": title},
        keys=("url",),
      )
    for result in search_results:
      candidate = {
        "turn": turn,
        "source": "search_result",
        "title": _compact(_string(result.get("title")), 180),
        "url": _string(result.get("href") or result.get("url")),
        "snippet": _compact(_string(result.get("snippet")), 280),
      }
      if candidate["url"] or candidate["title"]:
        self._append_unique(self.candidate_sources, candidate, keys=("url", "title"))
    for link in links[:12]:
      candidate = {
        "turn": turn,
        "source": "page_link",
        "title": _compact(_string(link.get("text") or link.get("title")), 180),
        "url": _string(link.get("href") or link.get("url")),
      }
      if candidate["url"] or candidate["title"]:
        self._append_unique(self.candidate_sources, candidate, keys=("url", "title"))
    if text or feed_titles or visible_cards:
      self._append_unique(
        self.evidence,
        {
          "turn": turn,
          "tool": record.name,
          "title": title,
          "url": url,
          "summary": _compact(" ".join(text.split()), 520),
          "feed_titles": feed_titles[:8],
          "visible_cards": visible_cards[:8],
        },
        keys=("url", "title", "summary"),
      )
    if search_results and not self._has_note("search_results_need_source_open"):
      self.strategy_notes.append("search_results_need_source_open: open promising candidate result pages before final answer.")
    if (feed_titles or visible_cards) and not self._has_note("dynamic_page_structured_cards"):
      self.strategy_notes.append("dynamic_page_structured_cards: use browser_execute_js for refresh/scroll/card JSON extraction if more detail is needed.")

  def _observe_non_page_output(self, record: ToolOutcomeView, *, turn: int) -> None:
    payload = _browser_js_payload(record.output)
    if payload is None:
      return
    if isinstance(payload, (list, dict)):
      text = _compact(str(payload), 700)
    else:
      text = _compact(str(payload), 700)
    if not text.strip():
      return
    self._append_unique(
      self.evidence,
      {
        "turn": turn,
        "tool": record.name,
        "summary": text,
      },
      keys=("tool", "summary"),
    )

  def _append_unique(self, items: list[dict[str, Any]], item: dict[str, Any], *, keys: tuple[str, ...]) -> None:
    identity = tuple(str(item.get(key) or "") for key in keys)
    if not any(tuple(str(existing.get(key) or "") for key in keys) == identity for existing in items):
      items.append(item)

  def _has_note(self, prefix: str) -> bool:
    return any(note.startswith(prefix) for note in self.strategy_notes)

  def _trim(self) -> None:
    self.visited_sources = self.visited_sources[-self.max_items :]
    self.evidence = self.evidence[-self.max_items :]
    self.candidate_sources = self.candidate_sources[-self.max_items :]
    self.failures = self.failures[-self.max_items :]
    self.strategy_notes = self.strategy_notes[-self.max_items :]


def _looks_like_research_goal(goal: str) -> bool:
  lowered = goal.lower()
  terms = (
    "搜索",
    "查询",
    "调研",
    "深度",
    "浏览器",
    "打开",
    "网页",
    "资料",
    "来源",
    "核实",
    "验证",
    "最新",
    "今天",
    "当前",
    "search",
    "research",
    "browser",
    "web",
  )
  return any(term in lowered for term in terms)


def _record_is_research_related(record: ToolOutcomeView) -> bool:
  haystack = f"{record.name} {record.capability_id}".lower()
  return any(term in haystack for term in ("browser", "http", "web", "search"))


def _record_url(output: dict[str, Any], input_value: dict[str, Any]) -> str | None:
  for source in (output, input_value, input_value.get("payload") if isinstance(input_value.get("payload"), dict) else {}):
    value = source.get("url") if isinstance(source, dict) else None
    if isinstance(value, str) and value.strip():
      return value.strip()
  page = output.get("page")
  if isinstance(page, dict):
    value = page.get("url")
    if isinstance(value, str) and value.strip():
      return value.strip()
  return None


def _record_title(output: dict[str, Any]) -> str | None:
  target = output.get("target")
  if isinstance(target, dict):
    label = target.get("label")
    if isinstance(label, str) and label.strip():
      return label.strip()
  page = output.get("page")
  if isinstance(page, dict):
    title = page.get("title")
    if isinstance(title, str) and title.strip():
      return title.strip()
  return None


def _browser_js_payload(output: dict[str, Any]) -> Any:
  for key in ("js_return", "data"):
    if key in output:
      return output.get(key)
  result = output.get("result")
  if isinstance(result, dict):
    for key in ("js_return", "data", "result"):
      if key in result:
        return result.get(key)
  return result


def _dict_list(value: Any) -> list[dict[str, Any]]:
  if not isinstance(value, list):
    return []
  return [item for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
  if not isinstance(value, list):
    return []
  return [_compact(str(item), 220) for item in value if isinstance(item, str) and item.strip()]


def _string(value: Any) -> str | None:
  if isinstance(value, str) and value.strip():
    return value.strip()
  return None


def _compact(value: str | None, limit: int) -> str:
  if value is None:
    return ""
  text = re.sub(r"\s+", " ", str(value)).strip()
  if len(text) <= limit:
    return text
  head = max(1, limit // 2)
  tail = max(1, limit - head - 5)
  return f"{text[:head]} ... {text[-tail:]}"
