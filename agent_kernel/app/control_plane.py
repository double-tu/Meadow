"""Control workbench assembly and health probing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_kernel.capabilities.adapters.control import (
  ADBMobileBackend,
  BrowserLinkHTTPBackend,
  ControlBackend,
  ControlTargetKind,
  ControlWorkbench,
  FakeControlBackend,
  Win32DesktopBackend,
)


@dataclass(slots=True)
class ControlBackendConfig:
  browser: dict[str, Any] = field(default_factory=dict)
  desktop: dict[str, Any] = field(default_factory=dict)
  mobile: dict[str, Any] = field(default_factory=dict)
  fake: bool = False

  @classmethod
  def from_dict(cls, data: dict[str, Any] | None) -> "ControlBackendConfig":
    raw = data or {}
    return cls(
      browser=raw.get("browser") if isinstance(raw.get("browser"), dict) else {},
      desktop=raw.get("desktop") if isinstance(raw.get("desktop"), dict) else {},
      mobile=raw.get("mobile") if isinstance(raw.get("mobile"), dict) else {},
      fake=bool(raw.get("fake", False)),
    )


class CompositeControlBackend:
  def __init__(self, backends: list[ControlBackend]) -> None:
    self._backends = backends

  def list_targets(self, kind: ControlTargetKind | None = None):
    targets = []
    for backend in self._backends:
      targets.extend(backend.list_targets(kind))
    return targets

  async def execute(self, command):
    for backend in self._backends:
      if command.target_kind in {target.kind for target in backend.list_targets(command.target_kind)} or command.target_id is None:
        result = await backend.execute(command)
        if result.ok or result.error is None or result.error.get("type") not in {
          "unsupported_control_target",
          "control_target_not_found",
        }:
          return result
    return await self._backends[0].execute(command)


class ControlPlaneService:
  def __init__(self, workbench: ControlWorkbench) -> None:
    self._workbench = workbench

  @classmethod
  def from_config(cls, config: ControlBackendConfig | dict[str, Any] | None = None) -> "ControlPlaneService":
    parsed = config if isinstance(config, ControlBackendConfig) else ControlBackendConfig.from_dict(config)
    backends: list[ControlBackend] = []
    if parsed.fake:
      backends.append(FakeControlBackend())
    if parsed.browser.get("enabled", True):
      backends.append(
        BrowserLinkHTTPBackend(
          base_url=str(parsed.browser.get("base_url") or "http://127.0.0.1:18766/link"),
          request_timeout_seconds=float(parsed.browser.get("request_timeout_seconds", 30.0)),
        )
      )
    if parsed.mobile.get("enabled", False):
      backends.append(
        ADBMobileBackend(
          adb_path=str(parsed.mobile.get("adb_path") or "adb"),
        )
      )
    if parsed.desktop.get("enabled", False):
      backends.append(Win32DesktopBackend())
    if not backends:
      backends.append(FakeControlBackend())
    return cls(ControlWorkbench(CompositeControlBackend(backends)))

  def list_targets(self, kind: ControlTargetKind | None = None) -> dict[str, Any]:
    return {"targets": [target.to_dict() for target in self._workbench.list_targets(kind)]}

  async def health(self) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for kind in ("browser", "desktop", "mobile"):
      try:
        targets = self._workbench.list_targets(kind)  # type: ignore[arg-type]
        checks[kind] = {"ok": True, "target_count": len(targets)}
      except Exception as exc:
        checks[kind] = {"ok": False, "error": str(exc)}
    return {"ok": any(check["ok"] for check in checks.values()), "checks": checks}

  @property
  def workbench(self) -> ControlWorkbench:
    return self._workbench
