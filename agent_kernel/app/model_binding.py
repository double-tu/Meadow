"""Model binding resolution for application services."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Protocol

from agent_kernel.config import LLMConfig
from agent_kernel.models import AnthropicMessagesProvider, GeminiProvider, ModelGateway, OpenAICompatibleProvider


@dataclass(slots=True)
class ModelBinding:
  gateway: ModelGateway
  provider_name: str
  model_ref: str
  config: LLMConfig | None = None


class ModelBindingProvider(Protocol):
  def resolve(self, agent_id: str) -> ModelBinding:
    ...


class StaticModelBindingProvider:
  """Provides an already-constructed gateway binding for tests or embedded hosts."""

  def __init__(self, gateway: ModelGateway, provider_name: str, model_ref: str) -> None:
    self._gateway = gateway
    self._provider_name = provider_name
    self._model_ref = model_ref

  def resolve(self, agent_id: str) -> ModelBinding:
    return ModelBinding(
      gateway=self._gateway,
      provider_name=self._provider_name,
      model_ref=self._model_ref,
    )


class ConfigModelBindingProvider:
  """Resolves per-agent model bindings from config center, defaults, and env."""

  def __init__(self, uow_factory, default_llm_config: LLMConfig | None = None) -> None:
    self._uow_factory = uow_factory
    self._default_llm_config = default_llm_config

  def resolve(self, agent_id: str) -> ModelBinding:
    config = self.load_config(agent_id)
    gateway = ModelGateway()
    gateway.register_provider(config.provider, provider_from_config(config))
    return ModelBinding(
      gateway=gateway,
      provider_name=config.provider,
      model_ref=config.model,
      config=config,
    )

  def load_config(self, agent_id: str) -> LLMConfig:
    with self._uow_factory() as uow:
      record = uow.interactions.get_record("config_section", "llm")
    data = record.get("data", {}) if isinstance(record, dict) and isinstance(record.get("data"), dict) else {}
    profile = llm_profile_from_config(data, agent_id=agent_id)
    defaults = self._default_llm_config
    provider = str(
      profile.get("provider")
      or data.get("provider")
      or (defaults.provider if defaults is not None else None)
      or os.getenv("AGENT_KERNEL_LLM_PROVIDER")
      or "openai-compatible"
    )
    model = (
      string_or_none(profile.get("model"))
      or string_or_none(data.get("model"))
      or (defaults.model if defaults is not None else None)
      or os.getenv("AGENT_KERNEL_LLM_MODEL")
      or os.getenv("OPENAI_MODEL")
    )
    api_key_env = string_or_none(profile.get("api_key_env")) or string_or_none(data.get("api_key_env"))
    api_key = os.getenv(api_key_env) if api_key_env else None
    api_key = (
      api_key
      or string_or_none(profile.get("api_key"))
      or string_or_none(data.get("api_key"))
      or (defaults.api_key if defaults is not None else None)
      or os.getenv("AGENT_KERNEL_LLM_API_KEY")
      or os.getenv("OPENAI_API_KEY")
      or os.getenv("GEMINI_API_KEY")
      or os.getenv("ANTHROPIC_API_KEY")
    )
    base_url = (
      string_or_none(profile.get("base_url"))
      or string_or_none(data.get("base_url"))
      or (defaults.base_url if defaults is not None else None)
      or os.getenv("AGENT_KERNEL_LLM_BASE_URL")
      or os.getenv("OPENAI_BASE_URL")
      or default_base_url(provider)
    )
    timeout = data.get(
      "timeout_seconds",
      profile.get(
        "timeout_seconds",
        defaults.timeout_seconds if defaults is not None else os.getenv("AGENT_KERNEL_LLM_TIMEOUT_SECONDS", 60),
      ),
    )
    missing = []
    if not model:
      missing.append("llm.model")
    if not api_key:
      missing.append("llm.api_key 或 llm.api_key_env")
    if missing:
      raise ValueError("缺少 " + "、".join(missing))
    return LLMConfig(
      provider=provider,
      model=model,
      api_key=api_key,
      base_url=str(base_url).rstrip("/"),
      timeout_seconds=float(timeout),
    )


def provider_from_config(config: LLMConfig):
  provider_kind = config.provider.lower()
  if provider_kind in {"openai-compatible", "openai", "deepseek", "new-api"}:
    return OpenAICompatibleProvider(
      api_key=config.api_key,
      base_url=config.base_url,
      timeout_seconds=config.timeout_seconds,
    )
  if provider_kind in {"gemini", "google-gemini"}:
    return GeminiProvider(
      api_key=config.api_key,
      base_url=config.base_url,
      timeout_seconds=config.timeout_seconds,
    )
  if provider_kind in {"anthropic", "claude"}:
    return AnthropicMessagesProvider(
      api_key=config.api_key,
      base_url=config.base_url,
      timeout_seconds=config.timeout_seconds,
    )
  raise ValueError(f"暂不支持的模型 provider 类型: {config.provider}")


def llm_profile_from_config(data: dict[str, Any], *, agent_id: str) -> dict[str, Any]:
  providers = data.get("providers")
  if not isinstance(providers, list) or not providers:
    return {}
  bindings = data.get("agent_bindings")
  binding = find_mapping(bindings, "agent_id", agent_id) if isinstance(bindings, list) else None
  provider_id = (
    string_or_none(binding.get("provider_id")) if binding else None
  ) or string_or_none(data.get("active_provider_id"))
  provider = find_mapping(providers, "provider_id", provider_id) if provider_id else None
  provider = provider or next((item for item in providers if isinstance(item, dict) and item.get("enabled", True)), None)
  if not isinstance(provider, dict):
    return {}
  models = provider.get("models")
  model_id = (
    string_or_none(binding.get("model_id")) if binding else None
  ) or string_or_none(data.get("active_model_id"))
  if not model_id and isinstance(models, list):
    for candidate in models:
      if isinstance(candidate, dict) and candidate.get("enabled", True):
        model_id = string_or_none(candidate.get("model_id"))
        break
  return {
    "provider": provider.get("kind") or provider.get("provider") or provider.get("platform"),
    "model": model_id,
    "api_key": provider.get("api_key"),
    "api_key_env": provider.get("api_key_env"),
    "base_url": provider.get("base_url"),
    "timeout_seconds": provider.get("timeout_seconds"),
  }


def find_mapping(items: object, key: str, value: str | None) -> dict[str, Any] | None:
  if not value or not isinstance(items, list):
    return None
  for item in items:
    if isinstance(item, dict) and item.get(key) == value:
      return item
  return None


def string_or_none(value: object) -> str | None:
  if value is None:
    return None
  text = str(value).strip()
  return text or None


def default_base_url(provider: str) -> str:
  provider_kind = provider.lower()
  if provider_kind in {"gemini", "google-gemini"}:
    return "https://generativelanguage.googleapis.com/v1beta"
  if provider_kind in {"anthropic", "claude"}:
    return "https://api.anthropic.com/v1"
  return "https://api.openai.com/v1"
