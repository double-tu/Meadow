"""Minimal policy engine for capability calls."""

from dataclasses import dataclass
from collections.abc import Callable
from enum import StrEnum

from agent_kernel.domain.base import utc_now
from agent_kernel.domain.capability import CapabilityGrant, CapabilitySpec, SideEffectLevel


class PolicyDecisionType(StrEnum):
  ALLOW = "allow"
  DENY = "deny"
  REQUIRE_APPROVAL = "require_approval"


@dataclass(slots=True)
class PolicyDecision:
  type: PolicyDecisionType
  reason: str | None = None
  grant: CapabilityGrant | None = None

  @property
  def allowed(self) -> bool:
    return self.type is PolicyDecisionType.ALLOW


class PolicyEngine:
  def __init__(
    self,
    grants: list[CapabilityGrant] | None = None,
    grants_provider: Callable[[str], list[CapabilityGrant]] | None = None,
  ) -> None:
    self._grants = grants or []
    self._grants_provider = grants_provider

  def decide(
    self,
    capability: CapabilitySpec,
    run_id: str,
    agent_id: str | None = None,
    task_id: str | None = None,
  ) -> PolicyDecision:
    if self._grants_provider is not None:
      self._grants = self._grants_provider(run_id)
    matching_grant = self._find_grant(capability, run_id, agent_id, task_id)
    if capability.required_grant and matching_grant is None:
      return PolicyDecision(
        type=PolicyDecisionType.DENY,
        reason=f"Missing required grant: {capability.required_grant}",
      )
    if matching_grant is not None and matching_grant.approval_required:
      return PolicyDecision(
        type=PolicyDecisionType.REQUIRE_APPROVAL,
        reason="Grant requires approval.",
        grant=matching_grant,
      )
    if capability.side_effect_level in {
      SideEffectLevel.EXEC,
      SideEffectLevel.EXTERNAL_MUTATION,
    } and matching_grant is None:
      return PolicyDecision(
        type=PolicyDecisionType.REQUIRE_APPROVAL,
        reason=f"High-risk capability requires approval: {capability.side_effect_level.value}",
      )
    return PolicyDecision(type=PolicyDecisionType.ALLOW, grant=matching_grant)

  def _find_grant(
    self,
    capability: CapabilitySpec,
    run_id: str,
    agent_id: str | None,
    task_id: str | None,
  ) -> CapabilityGrant | None:
    for grant in self._grants:
      if grant.expires_at <= utc_now():
        continue
      if grant.capability_id != capability.capability_id:
        continue
      if grant.run_id is not None and grant.run_id != run_id:
        continue
      if grant.agent_id is not None and grant.agent_id != agent_id:
        continue
      if grant.task_id is not None and grant.task_id != task_id:
        continue
      return grant
    return None
