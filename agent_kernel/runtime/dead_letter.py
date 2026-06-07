"""Dead letter helpers."""

from agent_kernel.domain.base import new_id
from agent_kernel.domain.stability import DeadLetterItem


def build_dead_letter(
  target_type: str,
  target_id: str,
  reason: str,
  failure_type: str,
  retryable: bool = False,
  event_refs: list[str] | None = None,
) -> DeadLetterItem:
  return DeadLetterItem(
    item_id=new_id("dead"),
    target_type=target_type,  # type: ignore[arg-type]
    target_id=target_id,
    reason=reason,
    failure_type=failure_type,
    retryable=retryable,
    event_refs=event_refs or [],
  )

