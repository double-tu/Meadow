"""Extension manifest domain models."""

from dataclasses import dataclass, field
from typing import Any, Literal

from agent_kernel.domain.base import DomainModel
from agent_kernel.domain.capability import SideEffectLevel


@dataclass(slots=True)
class ExtensionContribution(DomainModel):
  kind: Literal[
    "model_provider",
    "tool_provider",
    "mcp_server",
    "agent_preset",
    "workflow_template",
    "memory_backend",
    "checkpointer",
    "context_strategy",
    "policy_rule",
    "observability_sink",
    "eval_suite",
  ]
  name: str
  entrypoint: str
  config_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtensionManifest(DomainModel):
  extension_id: str
  name: str
  version: str
  compatible_kernel: str
  contributes: list[ExtensionContribution] = field(default_factory=list)
  permissions: list[str] = field(default_factory=list)
  runtime_requirements: dict[str, Any] = field(default_factory=dict)
  side_effect_level: SideEffectLevel | str = SideEffectLevel.NONE

  def __post_init__(self) -> None:
    if isinstance(self.side_effect_level, str):
      self.side_effect_level = SideEffectLevel(self.side_effect_level)

