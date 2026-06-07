import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_kernel.capabilities import CapabilityCallContext, CapabilityRegistry, CapabilityRuntime
from agent_kernel.capabilities.adapters import LocalToolExecutor
from agent_kernel.domain.capability import CapabilitySpec, SideEffectLevel
from agent_kernel.extensions import (
  ContributionRegistry,
  ExtensionManifestLoader,
  ExtensionPermissionMapper,
  ExtensionRuntime,
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

  def test_extension_runtime_imports_entrypoint_and_registers_tool_callable(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      package = Path(tmp) / "sample_ext.py"
      package.write_text(
        "\n".join(
          [
            "def provide_echo(context):",
            "    def echo(payload):",
            "        return {'echo': payload['text'], 'capability_id': context.capability_id}",
            "    return echo",
          ]
        ),
        encoding="utf-8",
      )
      sys.path.insert(0, tmp)
      try:
        data = manifest_data()
        data["contributes"][0]["name"] = "echo"
        data["contributes"][0]["entrypoint"] = "sample_ext:provide_echo"
        data["contributes"][0]["config_schema"] = {
          "input_schema": {"type": "object"},
          "output_schema": {"type": "object"},
        }
        data["side_effect_level"] = "none"
        data["permissions"] = []
        manifest = ExtensionManifestLoader().load_dict(data)
        capabilities = CapabilityRegistry()
        local_tools = LocalToolExecutor()
        contributions = ContributionRegistry()

        loaded = ExtensionRuntime().load_manifest(
          manifest,
          capability_registry=capabilities,
          local_tools=local_tools,
          contribution_registry=contributions,
        )
        runtime = CapabilityRuntime(capabilities, PolicyEngine(), local_tools)

        outcome = asyncio.run(
          runtime.call(
            "ext.local.echo",
            {"text": "hello"},
            CapabilityCallContext(run_id="run_extension"),
          )
        )
      finally:
        sys.path.remove(tmp)
        sys.modules.pop("sample_ext", None)

    self.assertTrue(loaded[0].registered)
    self.assertEqual(loaded[0].metadata["capability_id"], "ext.local.echo")
    self.assertTrue(outcome.result.ok)
    self.assertEqual(outcome.result.output["echo"], "hello")
    self.assertEqual(outcome.result.output["capability_id"], "ext.local.echo")

  def test_extension_runtime_supports_provider_object_register_method(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      package = Path(tmp) / "object_ext.py"
      package.write_text(
        "\n".join(
          [
            "class EchoProvider:",
            "    def register(self, context):",
            "        context.local_tools.register(context.capability_id, lambda payload: {'echo': payload['text']})",
          ]
        ),
        encoding="utf-8",
      )
      sys.path.insert(0, tmp)
      try:
        data = manifest_data()
        data["contributes"][0]["name"] = "echo_object"
        data["contributes"][0]["entrypoint"] = "object_ext:EchoProvider"
        data["contributes"][0]["config_schema"] = {
          "input_schema": {"type": "object"},
          "output_schema": {"type": "object"},
        }
        data["side_effect_level"] = "none"
        data["permissions"] = []
        manifest = ExtensionManifestLoader().load_dict(data)
        capabilities = CapabilityRegistry()
        local_tools = LocalToolExecutor()

        loaded = ExtensionRuntime().load_manifest(
          manifest,
          capability_registry=capabilities,
          local_tools=local_tools,
          contribution_registry=ContributionRegistry(),
        )
        runtime = CapabilityRuntime(capabilities, PolicyEngine(), local_tools)

        outcome = asyncio.run(
          runtime.call(
            "ext.local.echo_object",
            {"text": "hello"},
            CapabilityCallContext(run_id="run_extension_object"),
          )
        )
      finally:
        sys.path.remove(tmp)
        sys.modules.pop("object_ext", None)

    self.assertTrue(loaded[0].registered)
    self.assertEqual(outcome.result.output["echo"], "hello")


if __name__ == "__main__":
  unittest.main()
