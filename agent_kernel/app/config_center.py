"""Hot-updatable desktop configuration center."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from agent_kernel.domain.base import DomainModel, utc_now


SENSITIVE_KEYS = {"api_key", "password", "token", "secret", "client_secret"}


@dataclass(slots=True)
class ConfigSectionRecord(DomainModel):
  section: str
  data: dict[str, Any] = field(default_factory=dict)
  updated_at: datetime = field(default_factory=utc_now)


class ConfigCenterService:
  def __init__(self, uow_factory) -> None:
    self._uow_factory = uow_factory

  def list_sections(self, *, reveal_sensitive: bool = False) -> list[ConfigSectionRecord]:
    self._ensure_defaults()
    with self._uow_factory() as uow:
      records = uow.interactions.list_records("config_section")
    sections = [ConfigSectionRecord.from_dict(record) for record in records]
    if reveal_sensitive:
      return sections
    return [replace(section, data=_mask_sensitive(section.data)) for section in sections]

  def get_section(self, section: str, *, reveal_sensitive: bool = False) -> ConfigSectionRecord:
    self._ensure_defaults()
    with self._uow_factory() as uow:
      record = uow.interactions.get_record("config_section", section)
    if record is None:
      raise KeyError(f"Config section not found: {section}")
    parsed = ConfigSectionRecord.from_dict(record)
    return parsed if reveal_sensitive else replace(parsed, data=_mask_sensitive(parsed.data))

  def update_section(self, section: str, data: dict[str, Any], *, merge: bool = True) -> ConfigSectionRecord:
    if not section.strip():
      raise ValueError("section must be a non-empty string.")
    if not isinstance(data, dict):
      raise ValueError("data must be a JSON object.")
    current = None
    with self._uow_factory() as uow:
      record = uow.interactions.get_record("config_section", section)
      current = ConfigSectionRecord.from_dict(record) if record is not None else None
    next_data = {**(current.data if current and merge else {}), **data}
    updated = ConfigSectionRecord(section=section, data=next_data, updated_at=utc_now())
    with self._uow_factory() as uow:
      uow.interactions.save_record("config_section", section, updated)
    return replace(updated, data=_mask_sensitive(updated.data))

  def _ensure_defaults(self) -> None:
    with self._uow_factory() as uow:
      existing = {record["section"] for record in uow.interactions.list_records("config_section")}
      for section, data in _default_sections().items():
        if section not in existing:
          uow.interactions.save_record("config_section", section, ConfigSectionRecord(section=section, data=data))


def _default_sections() -> dict[str, dict[str, Any]]:
  return {
    "ui": {
      "locale": "zh-CN",
      "theme": "system",
      "density": "comfortable",
      "default_view": "chat",
    },
    "llm": {
      "provider": "openai-compatible",
      "model": "",
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY",
      "timeout_seconds": 60,
    },
    "agents": {
      "default_connector_id": "",
      "auto_delegate": False,
    },
    "mcp": {
      "auto_sync_to_agents": True,
    },
    "control": {
      "browser_enabled": False,
      "desktop_enabled": False,
      "mobile_enabled": False,
    },
  }


def _mask_sensitive(value: Any) -> Any:
  if isinstance(value, dict):
    masked = {}
    for key, item in value.items():
      if str(key).lower() in SENSITIVE_KEYS:
        masked[key] = "***" if item else ""
      else:
        masked[key] = _mask_sensitive(item)
    return masked
  if isinstance(value, list):
    return [_mask_sensitive(item) for item in value]
  return value
