"""Minimal model gateway."""

from agent_kernel.domain.context import ModelContext
from agent_kernel.models.provider import ModelProvider


class ModelGateway:
  def __init__(self) -> None:
    self._providers: dict[str, ModelProvider] = {}

  def register_provider(self, name: str, provider: ModelProvider) -> None:
    self._providers[name] = provider

  async def complete(self, provider_name: str, model_ref: str, context: ModelContext) -> dict[str, object]:
    try:
      provider = self._providers[provider_name]
    except KeyError as exc:
      raise KeyError(f"No model provider registered: {provider_name}") from exc
    return await provider.complete(model_ref, context)

