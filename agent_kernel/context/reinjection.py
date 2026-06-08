"""Post-compact state re-injection.

Compaction should reduce old text, not erase the active workbench state. This
module produces compact model-visible state anchors that can be inserted after a
summary or context rebuild.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReinjectableState:
  objective: str | None = None
  plan: list[str] = field(default_factory=list)
  active_skill_ids: list[str] = field(default_factory=list)
  active_task_ids: list[str] = field(default_factory=list)
  active_run_ids: list[str] = field(default_factory=list)
  active_workbench_ids: list[str] = field(default_factory=list)
  browser_targets: list[dict[str, Any]] = field(default_factory=list)
  pending_approvals: list[dict[str, Any]] = field(default_factory=list)
  pending_questions: list[dict[str, Any]] = field(default_factory=list)
  recent_failures: list[str] = field(default_factory=list)
  action_history: list[str] = field(default_factory=list)
  artifact_refs: list[dict[str, Any]] = field(default_factory=list)

  def is_empty(self) -> bool:
    return not any(
      [
        self.objective,
        self.plan,
        self.active_skill_ids,
        self.active_task_ids,
        self.active_run_ids,
        self.active_workbench_ids,
        self.browser_targets,
        self.pending_approvals,
        self.pending_questions,
        self.recent_failures,
        self.action_history,
        self.artifact_refs,
      ]
    )


class PostCompactStateReinjector:
  """Renders active state as a compact system message."""

  def render_message(self, state: ReinjectableState) -> dict[str, Any] | None:
    if state.is_empty():
      return None
    return {
      "role": "system",
      "content": {
        "type": "post_compact_state_reinjection",
        "objective": state.objective,
        "plan": state.plan[-12:],
        "active_skill_ids": state.active_skill_ids[-12:],
        "active_task_ids": state.active_task_ids[-12:],
        "active_run_ids": state.active_run_ids[-12:],
        "active_workbench_ids": state.active_workbench_ids[-12:],
        "browser_targets": state.browser_targets[-12:],
        "pending_approvals": state.pending_approvals[-8:],
        "pending_questions": state.pending_questions[-8:],
        "recent_failures": state.recent_failures[-10:],
        "action_history": state.action_history[-20:],
        "artifact_refs": state.artifact_refs[-20:],
        "instruction": (
          "Continue from this active state after compaction. Do not assume omitted details are lost; "
          "use memory/artifact/event/skill read tools to reopen details when needed."
        ),
      },
    }

  def inject(self, messages: list[dict[str, Any]], state: ReinjectableState) -> list[dict[str, Any]]:
    message = self.render_message(state)
    if message is None:
      return list(messages)
    system_prefix = [item for item in messages if item.get("role") == "system"]
    rest = [item for item in messages if item.get("role") != "system"]
    return [*system_prefix, message, *rest]
