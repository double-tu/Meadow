"""Goal evidence checks for continuous agent loops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ToolRecordView(Protocol):
  name: str
  ok: bool
  output: dict


@dataclass(frozen=True, slots=True)
class GoalEvidenceDecision:
  satisfied: bool
  reason: str = ""
  instruction: str = ""


class GoalEvidenceVerifier:
  """Detects when tool observations are sufficient for finalization.

  This verifier is intentionally conservative. It does not encode site-specific
  flows; it only recognizes generic evidence shapes returned by tools.
  """

  _FEED_GOAL_TERMS = ["推荐", "帖子", "内容", "列表", "feed", "最新", "刷新", "打开", "查看"]

  def evaluate(self, user_goal: str, tool_calls: list[ToolRecordView]) -> GoalEvidenceDecision:
    goal = user_goal.lower()
    if not any(term.lower() in goal for term in self._FEED_GOAL_TERMS):
      return GoalEvidenceDecision(False)
    feed_titles = self._browser_feed_titles(tool_calls)
    if len(feed_titles) >= 3:
      return GoalEvidenceDecision(
        True,
        reason=f"browser_feed_titles:{len(feed_titles)}",
        instruction=(
          "The latest browser observations already satisfy the user goal. "
          "Do not call more tools. Produce the final Chinese answer from the observed page/feed evidence, "
          "including the most relevant items and noting any uncertainty or access limitation."
        ),
      )
    visible_cards = self._browser_visible_cards(tool_calls)
    if len(visible_cards) >= 6 and any(term in goal for term in ["页面", "内容", "查看", "打开"]):
      return GoalEvidenceDecision(
        True,
        reason=f"browser_visible_cards:{len(visible_cards)}",
        instruction=(
          "The browser page observations contain enough visible page items for the requested inspection. "
          "Do not call more tools. Produce the final Chinese answer using the observed evidence."
        ),
      )
    return GoalEvidenceDecision(False)

  def _browser_feed_titles(self, tool_calls: list[ToolRecordView]) -> list[str]:
    titles: list[str] = []
    for call in tool_calls:
      if not call.ok:
        continue
      page = call.output.get("page")
      if not isinstance(page, dict):
        continue
      raw_titles = page.get("feed_titles")
      if isinstance(raw_titles, list):
        titles.extend(str(title).strip() for title in raw_titles if str(title).strip())
    return _unique(titles)

  def _browser_visible_cards(self, tool_calls: list[ToolRecordView]) -> list[str]:
    cards: list[str] = []
    for call in tool_calls:
      if not call.ok:
        continue
      page = call.output.get("page")
      if not isinstance(page, dict):
        continue
      raw_cards = page.get("visible_cards")
      if isinstance(raw_cards, list):
        cards.extend(str(card).strip() for card in raw_cards if str(card).strip())
    return _unique(cards)


def _unique(values: list[str]) -> list[str]:
  seen: set[str] = set()
  result: list[str] = []
  for value in values:
    if value in seen:
      continue
    seen.add(value)
    result.append(value)
  return result
