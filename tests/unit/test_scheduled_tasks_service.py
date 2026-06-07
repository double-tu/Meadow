from datetime import timedelta
import unittest

from agent_kernel.app.scheduled_tasks import ScheduledTaskService
from agent_kernel.domain.base import utc_now
from agent_kernel.persistence import connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class ScheduledTasksServiceTests(unittest.IsolatedAsyncioTestCase):
  async def test_create_run_due_and_persist_trigger_history(self) -> None:
    class Launcher:
      def __init__(self) -> None:
        self.payloads = []

      async def create_task(self, payload):
        self.payloads.append(payload)
        return {"ok": True, "task": {"run_id": payload["run_id"]}}

    conn = connect_sqlite()
    try:
      launcher = Launcher()
      service = ScheduledTaskService(unit_of_work_factory(conn), launcher)
      past = (utc_now() - timedelta(minutes=1)).isoformat()
      scheduled = service.create(
        {
          "task_id": "scheduled_once",
          "name": "daily report",
          "schedule_kind": "at",
          "schedule_value": past,
          "payload": {"title": "report", "run_id": "run_report"},
        }
      )

      triggers = await service.run_due()
      stored = service.get("scheduled_once")
      history = service.list_triggers("scheduled_once")

      self.assertEqual(scheduled.task_id, "scheduled_once")
      self.assertEqual(launcher.payloads, [{"title": "report", "run_id": "run_report"}])
      self.assertEqual(len(triggers), 1)
      self.assertEqual(triggers[0].result["task"]["run_id"], "run_report")
      self.assertFalse(stored.enabled)
      self.assertIsNone(stored.next_run_at)
      self.assertEqual(stored.trigger_count, 1)
      self.assertEqual(history[0].scheduled_task_id, "scheduled_once")
    finally:
      conn.close()

  async def test_every_schedule_reschedules_until_max_triggers(self) -> None:
    class Launcher:
      async def create_task(self, payload):
        return {"ok": True, "payload": payload}

    conn = connect_sqlite()
    try:
      service = ScheduledTaskService(unit_of_work_factory(conn), Launcher())
      service.create(
        {
          "task_id": "scheduled_every",
          "name": "poll",
          "schedule_kind": "every",
          "schedule_value": "1s",
          "payload": {"title": "poll"},
          "max_triggers": 2,
        }
      )

      await service.run_due(now=utc_now() + timedelta(seconds=2))
      await service.run_due(now=utc_now() + timedelta(seconds=4))
      stored = service.get("scheduled_every")

      self.assertFalse(stored.enabled)
      self.assertEqual(stored.trigger_count, 2)
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
