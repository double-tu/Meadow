"""Minimal model gateway."""

from agent_kernel.domain.context import ModelContext
from agent_kernel.models.health import ModelFailureKind, ModelHealthRouter, ModelRoute, classify_model_result
from agent_kernel.models.provider import ModelProvider


class ModelGateway:
  def __init__(self, health_router: ModelHealthRouter | None = None) -> None:
    self._providers: dict[str, ModelProvider] = {}
    self._health_router = health_router

  def register_provider(self, name: str, provider: ModelProvider) -> None:
    self._providers[name] = provider

  async def complete(self, provider_name: str, model_ref: str, context: ModelContext) -> dict[str, object]:
    route = ModelRoute(provider_name=provider_name, model_ref=model_ref)
    selected = self._health_router.choose(route) if self._health_router is not None else route
    try:
      return await self._complete_route(selected, context)
    except Exception:
      if self._health_router is None or not self._health_router.should_retry_with_fallback(selected):
        if self._health_router is not None:
          self._health_router.record_failure(selected, ModelFailureKind.PROVIDER_ERROR)
        raise
      fallback = self._health_router.choose(route)
      if fallback == selected:
        raise
      return await self._complete_route(fallback, context)

  async def _complete_route(self, route: ModelRoute, context: ModelContext) -> dict[str, object]:
    try:
      provider = self._providers[route.provider_name]
    except KeyError as exc:
      raise KeyError(f"No model provider registered: {route.provider_name}") from exc
    try:
      result = await provider.complete(route.model_ref, context)
    except TimeoutError:
      if self._health_router is not None:
        self._health_router.record_failure(route, ModelFailureKind.TIMEOUT)
      raise
    except Exception:
      if self._health_router is not None:
        self._health_router.record_failure(route, ModelFailureKind.PROVIDER_ERROR)
      raise
    failure = classify_model_result(result)
    if self._health_router is not None:
      if failure is None:
        self._health_router.record_success(route)
      else:
        self._health_router.record_failure(route, failure)
    return result
