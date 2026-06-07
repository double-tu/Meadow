"""Shared domain helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from agent_kernel.domain.serialization import from_dict, to_json, to_primitive


def utc_now() -> datetime:
  return datetime.now(UTC)


def new_id(prefix: str) -> str:
  return f"{prefix}_{uuid4().hex}"


@dataclass(slots=True)
class DomainModel:
  """Base class for serializable dataclass domain models."""

  def to_dict(self) -> dict[str, object]:
    value = to_primitive(self)
    if not isinstance(value, dict):
      raise TypeError("Domain model serialization must produce a dictionary.")
    return value

  def to_json(self) -> str:
    return to_json(self)

  @classmethod
  def from_dict(cls, data: dict[str, object]):
    return from_dict(cls, data)

