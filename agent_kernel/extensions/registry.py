"""Extension contribution registry."""

from dataclasses import dataclass, field

from agent_kernel.capabilities.registry import CapabilityRegistry
from agent_kernel.domain.capability import CapabilitySpec
from agent_kernel.domain.extension import ExtensionContribution, ExtensionManifest


@dataclass(slots=True)
class RegisteredContribution:
  extension_id: str
  contribution: ExtensionContribution


class ContributionRegistry:
  def __init__(self) -> None:
    self._contributions: dict[str, list[RegisteredContribution]] = {}

  def register_manifest(self, manifest: ExtensionManifest) -> None:
    for contribution in manifest.contributes:
      self._contributions.setdefault(contribution.kind, []).append(
        RegisteredContribution(extension_id=manifest.extension_id, contribution=contribution)
      )

  def list_by_kind(self, kind: str) -> list[RegisteredContribution]:
    return list(self._contributions.get(kind, []))

  def register_tool_capabilities(
    self,
    manifest: ExtensionManifest,
    capability_registry: CapabilityRegistry,
  ) -> None:
    for contribution in manifest.contributes:
      if contribution.kind != "tool_provider":
        continue
      capability_registry.register(
        CapabilitySpec(
          capability_id=f"{manifest.extension_id}.{contribution.name}",
          name=contribution.name,
          kind="tool",
          input_schema=contribution.config_schema.get("input_schema", {}),
          output_schema=contribution.config_schema.get("output_schema", {}),
          side_effect_level=manifest.side_effect_level,
          required_grant=contribution.config_schema.get("required_grant"),
        )
      )

