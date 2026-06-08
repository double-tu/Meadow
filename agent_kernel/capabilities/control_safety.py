"""Safety gates for browser/desktop/mobile control actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ControlPermissionTier(StrEnum):
  READ = "read"
  CLICK = "click"
  FULL = "full"


class ControlSafetyDecisionType(StrEnum):
  ALLOW = "allow"
  DENY = "deny"
  REQUIRE_APPROVAL = "require_approval"


@dataclass(slots=True)
class ControlSafetyDecision:
  type: ControlSafetyDecisionType
  reason: str
  metadata: dict[str, Any] = field(default_factory=dict)

  @property
  def allowed(self) -> bool:
    return self.type is ControlSafetyDecisionType.ALLOW


@dataclass(slots=True)
class ControlSafetyPolicy:
  enabled: bool = True
  denied_apps: set[str] = field(default_factory=set)
  app_tiers: dict[str, ControlPermissionTier] = field(default_factory=dict)
  dangerous_keys: set[str] = field(default_factory=lambda: {"Meta+Q", "Meta+Space", "Meta+Tab", "Ctrl+Meta+Q"})
  require_fresh_screenshot_for_input: bool = True


class ComputerUseSafetyGate:
  """Evaluates high-level safety constraints before concrete control adapters run."""

  def __init__(self, policy: ControlSafetyPolicy | None = None) -> None:
    self._policy = policy or ControlSafetyPolicy()

  def decide(self, *, action: str, target_kind: str, payload: dict[str, Any], context: dict[str, Any] | None = None) -> ControlSafetyDecision:
    if not self._policy.enabled:
      return ControlSafetyDecision(ControlSafetyDecisionType.DENY, "Computer/control use is disabled.")
    context = context or {}
    app_id = str(context.get("app_id") or "")
    if app_id and app_id in self._policy.denied_apps:
      return ControlSafetyDecision(ControlSafetyDecisionType.DENY, f"Application is denied: {app_id}")
    required = self._required_tier(action)
    tier = self._policy.app_tiers.get(app_id, ControlPermissionTier.FULL)
    if not self._tier_satisfies(tier, required):
      return ControlSafetyDecision(
        ControlSafetyDecisionType.REQUIRE_APPROVAL,
        f"Action {action} requires {required.value} tier but target has {tier.value}.",
        {"required_tier": required.value, "actual_tier": tier.value, "app_id": app_id},
      )
    key = payload.get("key")
    if action == "key" and isinstance(key, str) and key in self._policy.dangerous_keys:
      return ControlSafetyDecision(ControlSafetyDecisionType.DENY, f"Dangerous shortcut is blocked: {key}")
    if self._policy.require_fresh_screenshot_for_input and action in {"click", "tap", "type_text", "key"}:
      if context.get("screenshot_fresh") is False:
        return ControlSafetyDecision(
          ControlSafetyDecisionType.DENY,
          "Input action requires a fresh screenshot/UI observation.",
        )
    return ControlSafetyDecision(ControlSafetyDecisionType.ALLOW, "allowed")

  @staticmethod
  def _required_tier(action: str) -> ControlPermissionTier:
    if action in {"inspect_browser", "dump_ui", "screenshot", "list_targets"}:
      return ControlPermissionTier.READ
    if action in {"click", "tap", "scroll", "navigate"}:
      return ControlPermissionTier.CLICK
    return ControlPermissionTier.FULL

  @staticmethod
  def _tier_satisfies(actual: ControlPermissionTier, required: ControlPermissionTier) -> bool:
    order = {
      ControlPermissionTier.READ: 0,
      ControlPermissionTier.CLICK: 1,
      ControlPermissionTier.FULL: 2,
    }
    return order[actual] >= order[required]
