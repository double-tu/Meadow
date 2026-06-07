"""Context budget allocation."""

from dataclasses import dataclass


@dataclass(slots=True)
class ContextBudget:
  max_tokens: int
  message_tokens: int
  memory_tokens: int

  @classmethod
  def split(cls, max_tokens: int, memory_ratio: float = 0.4) -> "ContextBudget":
    memory_tokens = int(max_tokens * memory_ratio)
    return cls(
      max_tokens=max_tokens,
      message_tokens=max_tokens - memory_tokens,
      memory_tokens=memory_tokens,
    )

