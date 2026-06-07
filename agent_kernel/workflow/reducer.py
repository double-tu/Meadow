"""Workflow state reducer."""

from __future__ import annotations

from typing import Any


def reduce_state(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
  next_state = dict(current)
  for key, value in patch.items():
    if value is None:
      next_state.pop(key, None)
    else:
      next_state[key] = value
  return next_state

