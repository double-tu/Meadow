"""Dynamic extension entrypoint runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from typing import Any, Protocol

from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.capabilities.registry import CapabilityRegistry
from agent_kernel.domain.extension import ExtensionContribution, ExtensionManifest
from agent_kernel.extensions.registry import ContributionRegistry


@dataclass(slots=True)
class ExtensionContext:
  manifest: ExtensionManifest
  contribution: ExtensionContribution
  capability_registry: CapabilityRegistry | None = None
  local_tools: LocalToolExecutor | None = None
  contribution_registry: ContributionRegistry | None = None
  config: dict[str, Any] = field(default_factory=dict)

  @property
  def capability_id(self) -> str:
    return f"{self.manifest.extension_id}.{self.contribution.name}"


@dataclass(slots=True)
class LoadedExtensionContribution:
  extension_id: str
  contribution_name: str
  contribution_kind: str
  entrypoint: str
  provider: Any
  registered: bool = False
  metadata: dict[str, Any] = field(default_factory=dict)


class ExtensionEntrypointLoader(Protocol):
  def load(self, entrypoint: str) -> Any:
    ...


class ImportlibEntrypointLoader:
  """Loads `module:attribute` extension entrypoints using importlib."""

  def load(self, entrypoint: str) -> Any:
    module_name, separator, attribute_path = entrypoint.partition(":")
    if not separator or not module_name or not attribute_path:
      raise ValueError("Extension entrypoint must use 'module:attribute' format.")
    module = importlib.import_module(module_name)
    value: Any = module
    for part in attribute_path.split("."):
      value = getattr(value, part)
    return value


class ExtensionRuntime:
  """Loads extension entrypoints and lets them register runtime contributions."""

  def __init__(self, entrypoint_loader: ExtensionEntrypointLoader | None = None) -> None:
    self._entrypoint_loader = entrypoint_loader or ImportlibEntrypointLoader()

  def load_manifest(
    self,
    manifest: ExtensionManifest,
    *,
    capability_registry: CapabilityRegistry | None = None,
    local_tools: LocalToolExecutor | None = None,
    contribution_registry: ContributionRegistry | None = None,
    config: dict[str, Any] | None = None,
  ) -> list[LoadedExtensionContribution]:
    loaded: list[LoadedExtensionContribution] = []
    if contribution_registry is not None:
      contribution_registry.register_manifest(manifest)
      if capability_registry is not None:
        contribution_registry.register_tool_capabilities(manifest, capability_registry)
    for contribution in manifest.contributes:
      loaded.append(
        self.load_contribution(
          manifest,
          contribution,
          capability_registry=capability_registry,
          local_tools=local_tools,
          contribution_registry=contribution_registry,
          config=config or {},
        )
      )
    return loaded

  def load_contribution(
    self,
    manifest: ExtensionManifest,
    contribution: ExtensionContribution,
    *,
    capability_registry: CapabilityRegistry | None = None,
    local_tools: LocalToolExecutor | None = None,
    contribution_registry: ContributionRegistry | None = None,
    config: dict[str, Any] | None = None,
  ) -> LoadedExtensionContribution:
    entrypoint = self._entrypoint_loader.load(contribution.entrypoint)
    context = ExtensionContext(
      manifest=manifest,
      contribution=contribution,
      capability_registry=capability_registry,
      local_tools=local_tools,
      contribution_registry=contribution_registry,
      config=config or {},
    )
    provider = entrypoint() if isinstance(entrypoint, type) else entrypoint
    registered = self._register_provider(provider, context)
    return LoadedExtensionContribution(
      extension_id=manifest.extension_id,
      contribution_name=contribution.name,
      contribution_kind=contribution.kind,
      entrypoint=contribution.entrypoint,
      provider=provider,
      registered=registered,
      metadata={"capability_id": context.capability_id},
    )

  def _register_provider(self, provider: Any, context: ExtensionContext) -> bool:
    if hasattr(provider, "register"):
      provider.register(context)
      return True
    if callable(provider):
      result = provider(context)
      if result is not None and context.local_tools is not None and context.contribution.kind == "tool_provider":
        context.local_tools.register(context.capability_id, result)
      return True
    return False
