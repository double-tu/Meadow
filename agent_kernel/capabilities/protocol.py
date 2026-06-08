"""Capability runtime protocol helpers.

These helpers keep orchestration concerns outside concrete tool adapters. A
tool can remain a simple callable while the registry carries enough metadata
for schedulers, UI, and policy to reason about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_kernel.domain.capability import (
  CapabilityConcurrency,
  CapabilityInterruptBehavior,
  CapabilitySpec,
)


@dataclass(slots=True)
class CapabilityRuntimeProfile:
  capability_id: str
  name: str
  read_only: bool
  destructive: bool
  concurrency: CapabilityConcurrency
  requires_user_interaction: bool
  interrupt_behavior: CapabilityInterruptBehavior
  supports_streaming: bool
  supports_idempotency: bool
  resource_locks: list[str] = field(default_factory=list)
  render: dict[str, Any] = field(default_factory=dict)

  @property
  def can_run_concurrently(self) -> bool:
    return self.concurrency is CapabilityConcurrency.SAFE and not self.requires_user_interaction


class CapabilityProtocolDescriber:
  """Builds model/UI friendly profiles from registered capability specs."""

  def describe(self, spec: CapabilitySpec) -> CapabilityRuntimeProfile:
    hint = spec.execution_policy.render_hint
    return CapabilityRuntimeProfile(
      capability_id=spec.capability_id,
      name=spec.name,
      read_only=spec.is_read_only,
      destructive=spec.execution_policy.destructive,
      concurrency=spec.execution_policy.concurrency,
      requires_user_interaction=spec.requires_user_interaction,
      interrupt_behavior=spec.execution_policy.interrupt_behavior,
      supports_streaming=spec.supports_streaming,
      supports_idempotency=spec.supports_idempotency,
      resource_locks=list(spec.execution_policy.resource_locks),
      render={
        "display_name": hint.display_name or spec.name,
        "progress_label": hint.progress_label,
        "result_label": hint.result_label,
        "icon": hint.icon,
        "collapsed_by_default": hint.collapsed_by_default,
      },
    )


@dataclass(slots=True)
class CapabilityBatch:
  kind: str
  capability_ids: list[str]


class CapabilityBatchPlanner:
  """Partitions calls using capability concurrency declarations."""

  def plan(self, specs: list[CapabilitySpec]) -> list[CapabilityBatch]:
    batches: list[CapabilityBatch] = []
    current_parallel: list[str] = []
    for spec in specs:
      if spec.is_concurrency_safe and not spec.requires_user_interaction:
        current_parallel.append(spec.capability_id)
        continue
      if current_parallel:
        batches.append(CapabilityBatch(kind="parallel", capability_ids=current_parallel))
        current_parallel = []
      batches.append(CapabilityBatch(kind="sequential", capability_ids=[spec.capability_id]))
    if current_parallel:
      batches.append(CapabilityBatch(kind="parallel", capability_ids=current_parallel))
    return batches
