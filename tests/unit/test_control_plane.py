import unittest

from agent_kernel.app.control_plane import ControlPlaneService


class ControlPlaneServiceTests(unittest.IsolatedAsyncioTestCase):
  async def test_fake_control_plane_health_and_targets(self) -> None:
    service = ControlPlaneService.from_config({"fake": True, "browser": {"enabled": False}})

    health = await service.health()
    targets = service.list_targets()

    self.assertTrue(health["ok"])
    self.assertIn("browser", health["checks"])
    self.assertEqual(targets["targets"], [])


if __name__ == "__main__":
  unittest.main()
