"""Capability registry."""

from agent_kernel.domain.capability import CapabilitySpec


class CapabilityRegistry:
  def __init__(self) -> None:
    self._capabilities: dict[str, CapabilitySpec] = {}

  def register(self, spec: CapabilitySpec) -> None:
    self._capabilities[spec.capability_id] = spec

  def get(self, capability_id: str) -> CapabilitySpec:
    try:
      return self._capabilities[capability_id]
    except KeyError as exc:
      raise KeyError(f"Capability not registered: {capability_id}") from exc

  def list(self) -> list[CapabilitySpec]:
    return list(self._capabilities.values())

