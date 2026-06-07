"""Approximate token counting."""


def estimate_tokens(value: object) -> int:
  text = str(value)
  return max(1, len(text) // 4)

