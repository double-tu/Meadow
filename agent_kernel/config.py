"""Runtime configuration helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tomllib
from typing import Any


@dataclass(slots=True)
class LLMConfig:
  provider: str
  model: str
  api_key: str
  base_url: str = "https://api.openai.com/v1"
  timeout_seconds: float = 60.0

  @classmethod
  def load(cls, config_path: str | Path | None = None) -> "LLMConfig":
    path = config_path or os.getenv("AGENT_KERNEL_CONFIG")
    if path:
      return cls.from_file(path)
    return cls.from_env()

  @classmethod
  def from_env(cls) -> "LLMConfig":
    provider = os.getenv("AGENT_KERNEL_LLM_PROVIDER", "openai-compatible")
    model = os.getenv("AGENT_KERNEL_LLM_MODEL") or os.getenv("OPENAI_MODEL")
    api_key = os.getenv("AGENT_KERNEL_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = (
      os.getenv("AGENT_KERNEL_LLM_BASE_URL")
      or os.getenv("OPENAI_BASE_URL")
      or "https://api.openai.com/v1"
    )
    timeout_raw = os.getenv("AGENT_KERNEL_LLM_TIMEOUT_SECONDS", "60")

    missing = []
    if not model:
      missing.append("AGENT_KERNEL_LLM_MODEL or OPENAI_MODEL")
    if not api_key:
      missing.append("AGENT_KERNEL_LLM_API_KEY or OPENAI_API_KEY")
    if missing:
      raise ValueError(f"Missing LLM configuration: {', '.join(missing)}")

    return cls(
      provider=provider,
      model=model,
      api_key=api_key,
      base_url=base_url.rstrip("/"),
      timeout_seconds=float(timeout_raw),
    )

  @classmethod
  def from_file(cls, path: str | Path) -> "LLMConfig":
    config_path = Path(path)
    data = _read_config_file(config_path)
    raw_llm = data.get("llm", {})
    if not isinstance(raw_llm, dict):
      raise ValueError("Config file field [llm] must be an object/table.")

    provider = str(raw_llm.get("provider", "openai-compatible"))
    model = _string_or_none(raw_llm.get("model")) or os.getenv("AGENT_KERNEL_LLM_MODEL") or os.getenv("OPENAI_MODEL")
    api_key = _resolve_api_key(raw_llm)
    base_url = (
      _string_or_none(raw_llm.get("base_url"))
      or os.getenv("AGENT_KERNEL_LLM_BASE_URL")
      or os.getenv("OPENAI_BASE_URL")
      or "https://api.openai.com/v1"
    )
    timeout_raw = raw_llm.get("timeout_seconds", os.getenv("AGENT_KERNEL_LLM_TIMEOUT_SECONDS", 60))

    missing = []
    if not model:
      missing.append("llm.model")
    if not api_key:
      missing.append("llm.api_key or llm.api_key_env")
    if missing:
      raise ValueError(f"Missing LLM configuration in {config_path}: {', '.join(missing)}")

    return cls(
      provider=provider,
      model=model,
      api_key=api_key,
      base_url=base_url.rstrip("/"),
      timeout_seconds=float(timeout_raw),
    )


def _read_config_file(path: Path) -> dict[str, Any]:
  if not path.exists():
    raise FileNotFoundError(f"Config file not found: {path}")
  if path.suffix.lower() == ".json":
    with path.open("r", encoding="utf-8") as file:
      data = json.load(file)
  else:
    with path.open("rb") as file:
      data = tomllib.load(file)
  if not isinstance(data, dict):
    raise ValueError(f"Config file must contain an object/table: {path}")
  return data


def _resolve_api_key(raw_llm: dict[str, Any]) -> str | None:
  explicit = _string_or_none(raw_llm.get("api_key"))
  env_name = _string_or_none(raw_llm.get("api_key_env"))
  from_named_env = os.getenv(env_name) if env_name else None
  return (
    from_named_env
    or explicit
    or os.getenv("AGENT_KERNEL_LLM_API_KEY")
    or os.getenv("OPENAI_API_KEY")
  )


def _string_or_none(value: object) -> str | None:
  if value is None:
    return None
  text = str(value)
  return text if text else None
