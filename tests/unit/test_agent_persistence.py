import unittest

from agent_kernel.agents import build_mailbox_message, oneshot_agent_spec
from agent_kernel.agents.session import AgentSessionService
from agent_kernel.domain import AgentStatus, MailboxMessageStatus
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class AgentPersistenceTests(unittest.TestCase):
  def test_agent_session_and_mailbox_round_trip(self) -> None:
    conn = connect_sqlite()
    try:
      service = AgentSessionService(unit_of_work_factory(conn))
      session = service.create_session(oneshot_agent_spec("agent_1", "mock-small"))
      message = build_mailbox_message(
        recipient_session_id=session.session_id,
        mailbox_id=session.mailbox_id,
        content={"type": "task.assigned"},
      )

      with UnitOfWork(conn) as uow:
        uow.mailbox.send(message)

      with UnitOfWork(conn) as uow:
        restored = uow.agent_sessions.get(session.session_id)
        pending = uow.mailbox.list_pending(session.session_id)
        uow.mailbox.mark_handled(message.message_id)

      with UnitOfWork(conn) as uow:
        handled = uow.mailbox.list_pending(session.session_id)

      self.assertIsNotNone(restored)
      self.assertEqual(restored.status, AgentStatus.STARTING)
      self.assertEqual(pending[0].status, MailboxMessageStatus.PENDING)
      self.assertEqual(handled, [])
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()

