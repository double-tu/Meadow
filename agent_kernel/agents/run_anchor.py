"""Runtime working-memory anchor for continuous agent turns.

This module keeps the GenericAgent-inspired anchor mechanics independent from
tool execution: compact conversation history, per-turn action summaries, and
current progress are rendered as context, while concrete decisions remain with
the model and Skills/SOPs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Protocol


class TurnRecordLike(Protocol):
  name: str
  input: dict[str, Any]
  ok: bool
  error: dict[str, Any] | None


@dataclass(slots=True)
class RunAnchor:
  """Compact, model-visible state that survives each tool-result turn."""

  original_user_goal: str
  history_lines: list[str] = field(default_factory=list)
  max_recent_lines: int = 30

  @classmethod
  def from_messages(
    cls,
    *,
    original_user_goal: str,
    history_messages: list[dict[str, Any]],
  ) -> "RunAnchor":
    anchor = cls(original_user_goal=original_user_goal)
    for message in history_messages:
      role = message.get("role")
      content = message.get("content")
      if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
        continue
      prefix = "[USER]" if role == "user" else "[Agent]"
      anchor.history_lines.append(f"{prefix} {_compact_line(content, 180)}")
    anchor.history_lines.append(f"[USER] {_compact_line(original_user_goal, 220)}")
    return anchor

  def render_message(self, *, current_turn: int, action_history: list[str]) -> dict[str, Any]:
    earlier_lines = self.history_lines[:-self.max_recent_lines]
    recent_lines = self.history_lines[-self.max_recent_lines :]
    content: dict[str, Any] = {
      "type": "working_memory_anchor",
      "original_user_goal": self.original_user_goal,
      "current_turn": current_turn,
      "history": recent_lines,
      "action_history": action_history[-12:],
      "rules": [
        "Use this anchor to continue the same task instead of treating the latest short user reply as a new task.",
        "If repeated attempts do not add evidence, inspect state, switch capability/source, or ask the user with the concrete blocker.",
        "When a relevant Skill/SOP is needed, open it and store important constraints with memory_checkpoint for non-trivial tasks.",
      ],
    }
    if earlier_lines:
      content["earlier_context"] = self._fold_earlier(earlier_lines)
    return {"role": "system", "content": content}

  def record_turn(
    self,
    *,
    model_result: dict[str, Any],
    records: list[TurnRecordLike],
  ) -> None:
    summary = _extract_model_summary(model_result)
    if summary is None and records:
      summary = _tool_summary(records[0])
    if summary is None:
      summary = "直接回答了用户问题"
    self.history_lines.append(f"[Agent] {_compact_line(summary, 160)}")

  @staticmethod
  def _fold_earlier(lines: list[str]) -> list[str]:
    folded: list[str] = []
    agent_count = 0
    last_agent = ""

    def flush_agent_count() -> None:
      nonlocal agent_count, last_agent
      if agent_count <= 0:
        return
      if last_agent:
        folded.append(f"{last_agent}（{agent_count} turns）")
      else:
        folded.append(f"[Agent]（{agent_count} turns）")
      agent_count = 0
      last_agent = ""

    for line in lines:
      if line.startswith("[USER]"):
        flush_agent_count()
        folded.append(line)
        continue
      agent_count += 1
      last_agent = line
    flush_agent_count()
    return folded[-70:]


def _extract_model_summary(result: dict[str, Any]) -> str | None:
  output = result.get("output", result.get("content"))
  candidates: list[str] = []
  if isinstance(output, dict):
    for key in ("summary", "content", "value"):
      value = output.get(key)
      if isinstance(value, str):
        candidates.append(value)
  elif isinstance(output, str):
    candidates.append(output)
  for value in candidates:
    match = re.search(r"<summary>\s*(.*?)\s*</summary>", value, flags=re.DOTALL | re.IGNORECASE)
    if match and match.group(1).strip():
      return match.group(1).strip()
  for value in candidates:
    stripped = _strip_internal_blocks(value).strip()
    if stripped:
      return stripped
  return None


def _tool_summary(record: TurnRecordLike) -> str:
  args = {
    key: value
    for key, value in record.input.items()
    if not key.startswith("_") and key not in {"code", "content", "body"}
  }
  status = "成功" if record.ok else f"失败:{(record.error or {}).get('type', 'unknown')}"
  if args:
    return f"调用工具{record.name} {status}, args: {_compact_line(str(args), 80)}"
  return f"调用工具{record.name} {status}"


def _strip_internal_blocks(value: str) -> str:
  value = re.sub(r"```[\s\S]*?```", "", value)
  value = re.sub(r"<thinking>[\s\S]*?</thinking>", "", value, flags=re.IGNORECASE)
  value = re.sub(r"<summary>[\s\S]*?</summary>", "", value, flags=re.IGNORECASE)
  return value


def _compact_line(value: str, limit: int) -> str:
  line = " ".join(str(value).split())
  if len(line) <= limit:
    return line
  head = max(1, limit // 2)
  tail = max(1, limit - head - 5)
  return f"{line[:head]} ... {line[-tail:]}"
