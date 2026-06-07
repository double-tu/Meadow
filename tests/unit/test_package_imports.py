import importlib
import unittest


class PackageImportTests(unittest.TestCase):
  def test_top_level_packages_import(self) -> None:
    packages = [
      "agent_kernel",
      "agent_kernel.agents",
      "agent_kernel.app",
      "agent_kernel.autonomy",
      "agent_kernel.capabilities",
      "agent_kernel.context",
      "agent_kernel.domain",
      "agent_kernel.evaluation",
      "agent_kernel.extensions",
      "agent_kernel.hosts",
      "agent_kernel.memory",
      "agent_kernel.models",
      "agent_kernel.observability",
      "agent_kernel.persistence",
      "agent_kernel.policy",
      "agent_kernel.runtime",
      "agent_kernel.workflow",
    ]

    for package in packages:
      with self.subTest(package=package):
        self.assertIsNotNone(importlib.import_module(package))


if __name__ == "__main__":
  unittest.main()

