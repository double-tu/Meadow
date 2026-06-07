import json
import tempfile
import unittest
from pathlib import Path

from agent_kernel.capabilities import CapabilityRegistry
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel
from agent_kernel.extensions import (
  ContributionRegistry,
  ExtensionManifestLoader,
  ExtensionPermissionMapper,
)
from agent_kernel.policy import PolicyDecisionType, PolicyEngine


def manifest_data() -> dict[str, object]:
  return {
    "extension_id": "ext.local",
    "name": "Local Tools",
    "version": "0.1.0",
    "compatible_kernel": ">=0.1.0",
    "side_effect_level": "write",
    "permissions": ["capability:ext.local.write_file"],
    "contributes": [
      {
        "kind": "tool_provider",
        "name": "write_file",
        "entrypoint": "example:write_file",
        "config_schema": {
          "input_schema": {"type": "object"},
          "output_schema": {"type": "object"},
          "required_grant": "fs.write",
        },
      }
    ],
  }


class ExtensionTests(unittest.TestCase):
  def test_manifest_loader_loads_dict_and_json(self) -> None:
    loader = ExtensionManifestLoader()
    manifest = loader.load_dict(manifest_data())

    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "extension.json"
      path.write_text(json.dumps(manifest_data()), encoding="utf-8")
      loaded = loader.load_json(path)

    self.assertEqual(manifest.extension_id, "ext.local")
    self.assertEqual(loaded.contributes[0].name, "write_file")

  def test_contribution_registry_registers_tool_capability_metadata(self) -> None:
    manifest = ExtensionManifestLoader().load_dict(manifest_data())
    contributions = ContributionRegistry()
    capabilities = CapabilityRegistry()

    contributions.register_manifest(manifest)
    contributions.register_tool_capabilities(manifest, capabilities)

    registered = contributions.list_by_kind("tool_provider")
    spec = capabilities.get("ext.local.write_file")

    self.assertEqual(registered[0].extension_id, "ext.local")
    self.assertEqual(spec.side_effect_level, SideEffectLevel.WRITE)
    self.assertEqual(spec.required_grant, "fs.write")

  def test_extension_permissions_map_to_policy_grants(self) -> None:
    manifest = ExtensionManifestLoader().load_dict(manifest_data())
    grants = ExtensionPermissionMapper().grants_for_manifest(manifest, run_id="run_1")
    spec = CapabilitySpec(
      capability_id="ext.local.write_file",
      name="write_file",
      kind="tool",
      input_schema={},
      output_schema={},
      side_effect_level=SideEffectLevel.WRITE,
      required_grant="fs.write",
    )

    decision = PolicyEngine(grants=grants).decide(spec, run_id="run_1")

    self.assertEqual(grants[0].capability_id, "ext.local.write_file")
    self.assertEqual(decision.type, PolicyDecisionType.ALLOW)


if __name__ == "__main__":
  unittest.main()

