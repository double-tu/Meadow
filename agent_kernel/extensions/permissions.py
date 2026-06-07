"""Extension permission mapping."""

from datetime import timedelta

from agent_kernel.domain.base import new_id, utc_now
from agent_kernel.domain.capability import CapabilityGrant
from agent_kernel.domain.extension import ExtensionManifest


class ExtensionPermissionMapper:
  def grants_for_manifest(
    self,
    manifest: ExtensionManifest,
    run_id: str,
    ttl_seconds: int = 300,
  ) -> list[CapabilityGrant]:
    expires_at = utc_now() + timedelta(seconds=ttl_seconds)
    grants: list[CapabilityGrant] = []
    for permission in manifest.permissions:
      if permission.startswith("capability:"):
        grants.append(
          CapabilityGrant(
            grant_id=new_id("grant"),
            capability_id=permission.removeprefix("capability:"),
            run_id=run_id,
            expires_at=expires_at,
            approval_required=False,
          )
        )
    return grants

