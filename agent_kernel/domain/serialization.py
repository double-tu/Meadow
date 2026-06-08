"""Small dataclass serialization layer used before adding heavier dependencies."""

from __future__ import annotations

import json
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from enum import Enum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints


def to_primitive(value: Any) -> Any:
  if is_dataclass(value):
    return {field.name: to_primitive(getattr(value, field.name)) for field in fields(value)}
  if isinstance(value, datetime):
    return value.isoformat()
  if isinstance(value, Enum):
    return value.value
  if isinstance(value, list):
    return [to_primitive(item) for item in value]
  if isinstance(value, tuple):
    return [to_primitive(item) for item in value]
  if isinstance(value, set):
    return [to_primitive(item) for item in sorted(value, key=str)]
  if isinstance(value, dict):
    return {str(key): to_primitive(item) for key, item in value.items()}
  return value


def to_json(value: Any) -> str:
  return json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True)


def from_dict(cls: type[Any], data: dict[str, Any]) -> Any:
  if not is_dataclass(cls):
    raise TypeError(f"{cls!r} is not a dataclass type.")

  hints = get_type_hints(cls)
  kwargs: dict[str, Any] = {}
  for field in fields(cls):
    if field.name not in data:
      if field.default is not MISSING or field.default_factory is not MISSING:  # type: ignore[attr-defined]
        continue
      raise TypeError(f"Missing required field: {field.name}")
    kwargs[field.name] = _from_value(hints.get(field.name, Any), data[field.name])
  return cls(**kwargs)


def _from_value(expected_type: Any, value: Any) -> Any:
  if value is None or expected_type is Any:
    return value

  origin = get_origin(expected_type)
  args = get_args(expected_type)

  if origin in (Union, UnionType):
    non_none_args = [arg for arg in args if arg is not type(None)]
    for arg in non_none_args:
      try:
        return _from_value(arg, value)
      except (TypeError, ValueError):
        continue
    return value

  if origin is Literal:
    if value not in args:
      raise ValueError(f"Expected one of {args!r}, got {value!r}.")
    return value

  if origin is list:
    item_type = args[0] if args else Any
    return [_from_value(item_type, item) for item in value]

  if origin is dict:
    value_type = args[1] if len(args) > 1 else Any
    return {str(key): _from_value(value_type, item) for key, item in value.items()}

  if expected_type is datetime:
    return datetime.fromisoformat(value)

  if isinstance(expected_type, type) and issubclass(expected_type, Enum):
    return expected_type(value)

  if isinstance(expected_type, type) and is_dataclass(expected_type):
    return from_dict(expected_type, value)

  return value
