"""Scheduled task domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel, new_id, utc_now
from agent_kernel.domain.errors import DomainValidationError


ScheduleKind = Literal["at", "every", "cron"]


@dataclass(slots=True)
class ScheduledTask(DomainModel):
  name: str
  schedule_kind: ScheduleKind | str
  schedule_value: str
  payload: dict[str, Any]
  task_id: str = field(default_factory=lambda: new_id("scheduled_task"))
  enabled: bool = True
  next_run_at: datetime | None = None
  last_run_at: datetime | None = None
  trigger_count: int = 0
  max_triggers: int | None = None
  metadata: dict[str, Any] = field(default_factory=dict)
  created_at: datetime = field(default_factory=utc_now)
  updated_at: datetime = field(default_factory=utc_now)

  def __post_init__(self) -> None:
    if not self.name.strip():
      raise DomainValidationError("ScheduledTask.name is required.")
    if self.schedule_kind not in {"at", "every", "cron"}:
      raise DomainValidationError("ScheduledTask.schedule_kind must be at, every, or cron.")
    if not self.schedule_value.strip():
      raise DomainValidationError("ScheduledTask.schedule_value is required.")
    if not isinstance(self.payload, dict):
      raise DomainValidationError("ScheduledTask.payload must be a dictionary.")
    if self.max_triggers is not None and self.max_triggers <= 0:
      raise DomainValidationError("ScheduledTask.max_triggers must be positive.")
    if self.enabled and self.next_run_at is None:
      self.next_run_at = compute_next_run_at(self.schedule_kind, self.schedule_value, utc_now())

  @property
  def due(self) -> bool:
    return self.enabled and self.next_run_at is not None and self.next_run_at <= utc_now()

  def mark_triggered(self, now: datetime | None = None) -> "ScheduledTask":
    current = now or utc_now()
    self.last_run_at = current
    self.trigger_count += 1
    self.updated_at = current
    if self.max_triggers is not None and self.trigger_count >= self.max_triggers:
      self.enabled = False
      self.next_run_at = None
    elif self.schedule_kind == "at":
      self.enabled = False
      self.next_run_at = None
    else:
      self.next_run_at = compute_next_run_at(self.schedule_kind, self.schedule_value, current)
    return self


@dataclass(slots=True)
class ScheduledTaskTrigger(DomainModel):
  scheduled_task_id: str
  result: dict[str, Any]
  trigger_id: str = field(default_factory=lambda: new_id("scheduled_trigger"))
  triggered_at: datetime = field(default_factory=utc_now)


def compute_next_run_at(schedule_kind: str, schedule_value: str, base_time: datetime) -> datetime:
  if schedule_kind == "at":
    return _parse_datetime(schedule_value)
  if schedule_kind == "every":
    return base_time + _parse_interval(schedule_value)
  if schedule_kind == "cron":
    return _next_simple_cron(schedule_value, base_time)
  raise DomainValidationError("Unsupported schedule kind.")


def _parse_datetime(value: str) -> datetime:
  text = value.strip()
  if text.endswith("Z"):
    text = text[:-1] + "+00:00"
  try:
    parsed = datetime.fromisoformat(text)
  except ValueError as exc:
    raise DomainValidationError("at schedule_value must be an ISO datetime.") from exc
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=utc_now().tzinfo)
  return parsed


def _parse_interval(value: str) -> timedelta:
  text = value.strip().lower()
  units = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
  }
  for suffix, multiplier in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
    if text.endswith(suffix):
      raw = text[: -len(suffix)].strip()
      if not raw:
        break
      seconds = float(raw) * multiplier
      if seconds <= 0:
        break
      return timedelta(seconds=seconds)
  raise DomainValidationError("every schedule_value must be a positive interval like 30s, 5m, or 1h.")


def _next_simple_cron(value: str, base_time: datetime) -> datetime:
  fields = value.split()
  if len(fields) != 5:
    raise DomainValidationError("cron schedule_value must have 5 fields.")
  minute, hour = fields[0], fields[1]
  if not (_cron_field_supported(minute, 0, 59) and _cron_field_supported(hour, 0, 23)):
    raise DomainValidationError("cron minute/hour fields support only '*' or a single integer.")
  candidate = (base_time + timedelta(minutes=1)).replace(second=0, microsecond=0)
  for _ in range(60 * 24 * 366):
    if _cron_matches(candidate.minute, minute) and _cron_matches(candidate.hour, hour):
      return candidate
    candidate += timedelta(minutes=1)
  raise DomainValidationError("cron schedule could not be resolved.")


def _cron_field_supported(value: str, minimum: int, maximum: int) -> bool:
  if value == "*":
    return True
  if not value.isdigit():
    return False
  parsed = int(value)
  return minimum <= parsed <= maximum


def _cron_matches(actual: int, spec: str) -> bool:
  return spec == "*" or actual == int(spec)
