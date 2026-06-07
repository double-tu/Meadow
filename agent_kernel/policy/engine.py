"""Minimal policy engine for capability calls."""

from dataclasses import dataclass
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

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


class CapabilityScopeResolver(Protocol):
  """Extract policy-relevant targets from a capability input payload."""

  def filesystem_targets(self, capability: CapabilitySpec, input: dict[str, Any]) -> list[str]:
    """Return filesystem paths the capability intends to touch."""

  def network_targets(self, capability: CapabilitySpec, input: dict[str, Any]) -> list[str]:
    """Return network targets the capability intends to contact."""


class DefaultCapabilityScopeResolver:
  """Conservative resolver for common tool payload shapes.

  Adapters can keep their own domain-specific input names while still using
  policy scopes by passing explicit ``path``/``paths`` and ``url``/``urls``.
  """

  def filesystem_targets(self, capability: CapabilitySpec, input: dict[str, Any]) -> list[str]:
    return self._strings_from(input, "path", "paths")

  def network_targets(self, capability: CapabilitySpec, input: dict[str, Any]) -> list[str]:
    return self._strings_from(input, "url", "urls")

  @staticmethod
  def _strings_from(input: dict[str, Any], singular_key: str, plural_key: str) -> list[str]:
    values: list[str] = []
    singular = input.get(singular_key)
    if isinstance(singular, str):
      values.append(singular)
    plural = input.get(plural_key)
    if isinstance(plural, list):
      values.extend(value for value in plural if isinstance(value, str))
    return values


class PolicyEngine:
  def __init__(
    self,
    grants: list[CapabilityGrant] | None = None,
    grants_provider: Callable[[str], list[CapabilityGrant]] | None = None,
    scope_resolver: CapabilityScopeResolver | None = None,
  ) -> None:
    self._grants = grants or []
    self._grants_provider = grants_provider
    self._scope_resolver = scope_resolver or DefaultCapabilityScopeResolver()

  def decide(
    self,
    capability: CapabilitySpec,
    run_id: str,
    agent_id: str | None = None,
    task_id: str | None = None,
    input: dict[str, Any] | None = None,
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
    scope_denial = self._scope_denial(capability, matching_grant, input or {})
    if scope_denial is not None:
      return scope_denial
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

  def _scope_denial(
    self,
    capability: CapabilitySpec,
    grant: CapabilityGrant | None,
    input: dict[str, Any],
  ) -> PolicyDecision | None:
    if grant is None:
      return None
    filesystem_targets = self._scope_resolver.filesystem_targets(capability, input)
    if filesystem_targets and grant.filesystem_scope:
      denied_path = self._first_path_outside_scope(filesystem_targets, grant.filesystem_scope)
      if denied_path is not None:
        return PolicyDecision(
          type=PolicyDecisionType.DENY,
          reason=f"Filesystem target outside granted scope: {denied_path}",
          grant=grant,
        )
    network_targets = self._scope_resolver.network_targets(capability, input)
    if network_targets and grant.network_scope:
      denied_target = self._first_network_target_outside_scope(network_targets, grant.network_scope)
      if denied_target is not None:
        return PolicyDecision(
          type=PolicyDecisionType.DENY,
          reason=f"Network target outside granted scope: {denied_target}",
          grant=grant,
        )
    return None

  @staticmethod
  def _first_path_outside_scope(targets: list[str], scopes: list[str]) -> str | None:
    resolved_scopes = [Path(scope).expanduser().resolve(strict=False) for scope in scopes]
    for target in targets:
      resolved_target = Path(target).expanduser().resolve(strict=False)
      if not any(resolved_target == scope or scope in resolved_target.parents for scope in resolved_scopes):
        return target
    return None

  @staticmethod
  def _first_network_target_outside_scope(targets: list[str], scopes: list[str]) -> str | None:
    normalized_scopes = {PolicyEngine._normalize_network_scope(scope) for scope in scopes}
    for target in targets:
      normalized_target = PolicyEngine._normalize_network_scope(target)
      if normalized_target not in normalized_scopes:
        return target
    return None

  @staticmethod
  def _normalize_network_scope(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname if parsed.hostname else value.split("/", 1)[0]
    return host.lower()
