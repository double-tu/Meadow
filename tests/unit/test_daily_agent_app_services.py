from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http.client import IncompleteRead
import unittest

from agent_kernel.app.config_center import ConfigCenterService
from agent_kernel.app.conversation_task_hub import ConversationTaskHub
from agent_kernel.app.daily_agent import DailyAgentRequest, DailyAgentResponse
from agent_kernel.app.desktop_chat import DesktopChatService
from agent_kernel.app.model_binding import ConfigModelBindingProvider, ModelBinding
from agent_kernel.config import LLMConfig
from agent_kernel.models import MockModelProvider, ModelGateway
from agent_kernel.persistence import connect_sqlite
from agent_kernel.runtime import unit_of_work_factory


class FakeLauncher:
  async def create_task(self, payload):
    return {"task": {"run_id": payload["run_id"], "title": payload["title"]}}


class FakeDailyAgentExecutor:
  def __init__(self) -> None:
    self.requests: list[DailyAgentRequest] = []
    self.gateway_calls: list[ModelGateway] = []

  async def execute(self, request: DailyAgentRequest, gateway: ModelGateway) -> DailyAgentResponse:
    self.requests.append(request)
    self.gateway_calls.append(gateway)
    return DailyAgentResponse(
      content="executor response",
      raw={"daily_agent": {"run_id": request.run_id, "status": "completed", "pending": None}},
    )


class WaitingDailyAgentExecutor:
  def __init__(self) -> None:
    self.started: asyncio.Future[DailyAgentRequest] | None = None
    self.release: asyncio.Event | None = None

  async def execute(self, request: DailyAgentRequest, gateway: ModelGateway) -> DailyAgentResponse:
    loop = asyncio.get_running_loop()
    if self.started is None:
      self.started = loop.create_future()
    if self.release is None:
      self.release = asyncio.Event()
    self.started.set_result(request)
    await self.release.wait()
    status = "cancelled" if request.cancel_requested is not None and request.cancel_requested() else "completed"
    return DailyAgentResponse(
      content="cancelled" if status == "cancelled" else "completed",
      raw={"daily_agent": {"run_id": request.run_id, "status": status, "pending": None}},
    )


class FailingDailyAgentExecutor:
  async def execute(self, request: DailyAgentRequest, gateway: ModelGateway) -> DailyAgentResponse:
    raise IncompleteRead(b"x" * 12, 20)


@dataclass(slots=True)
class StaticBindingProvider:
  binding: ModelBinding

  def resolve(self, agent_id: str) -> ModelBinding:
    return self.binding


class DailyAgentAppServiceTests(unittest.IsolatedAsyncioTestCase):
  async def test_daily_agent_request_defaults_to_research_sized_turn_budget(self) -> None:
    request = DailyAgentRequest(session_id="chat", run_id="run", user_content="search")

    self.assertEqual(request.max_turns, 16)

  async def test_desktop_chat_delegates_daily_agent_execution_through_interface(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      gateway.register_provider("mock", MockModelProvider())
      executor = FakeDailyAgentExecutor()
      service = DesktopChatService(
        uow_factory,
        FakeLauncher(),
        conversation_task_hub=ConversationTaskHub(uow_factory, FakeLauncher()),
        model_binding_provider=StaticBindingProvider(ModelBinding(gateway, "mock", "mock-model")),
        daily_agent_executor=executor,
      )
      session = service.create_session({"title": "日常对话"})

      sent = await service.send_message(session.session_id, {"content": "请创建工作台", "run_id": "run_iface"})

      self.assertEqual(sent["messages"][1]["content"], "executor response")
      self.assertEqual(executor.requests[0].run_id, "run_iface")
      self.assertEqual(executor.requests[0].session_id, session.session_id)
      self.assertEqual(executor.requests[0].provider_name, "mock")
      self.assertEqual(executor.requests[0].model_ref, "mock-model")
      self.assertIs(executor.gateway_calls[0], gateway)
    finally:
      conn.close()

  async def test_desktop_chat_pause_marks_running_daily_agent_cancelled(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      gateway.register_provider("mock", MockModelProvider())
      executor = WaitingDailyAgentExecutor()
      service = DesktopChatService(
        uow_factory,
        FakeLauncher(),
        conversation_task_hub=ConversationTaskHub(uow_factory, FakeLauncher()),
        model_binding_provider=StaticBindingProvider(ModelBinding(gateway, "mock", "mock-model")),
        daily_agent_executor=executor,
      )
      session = service.create_session({"title": "日常对话"})

      send_task = asyncio.create_task(
        service.send_message(session.session_id, {"content": "执行长任务", "run_id": "run_pause"})
      )
      while executor.started is None:
        await asyncio.sleep(0)
      request = await executor.started
      paused = service.pause(session.session_id)
      assert executor.release is not None
      executor.release.set()
      sent = await send_task

      self.assertEqual(request.run_id, "run_pause")
      self.assertEqual(paused["session"]["status"], "paused")
      self.assertEqual(sent["messages"][1]["content"], "cancelled")
      self.assertEqual(sent["session"]["status"], "paused")
    finally:
      conn.close()

  async def test_desktop_chat_reports_incomplete_read_without_raw_exception_blob(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      gateway = ModelGateway()
      gateway.register_provider("mock", MockModelProvider())
      service = DesktopChatService(
        uow_factory,
        FakeLauncher(),
        conversation_task_hub=ConversationTaskHub(uow_factory, FakeLauncher()),
        model_binding_provider=StaticBindingProvider(ModelBinding(gateway, "mock", "mock-model")),
        daily_agent_executor=FailingDailyAgentExecutor(),
      )
      session = service.create_session({"title": "日常对话"})

      sent = await service.send_message(session.session_id, {"content": "执行任务", "run_id": "run_incomplete"})

      self.assertIn("模型或网络响应未完整返回", sent["messages"][1]["content"])
      self.assertNotIn("IncompleteRead(", sent["messages"][1]["content"])
      self.assertEqual(sent["messages"][1]["metadata"]["llm_result"]["error"]["type"], "incomplete_read")
      self.assertEqual(sent["messages"][1]["metadata"]["llm_result"]["error"]["read_bytes"], 12)
    finally:
      conn.close()


class ModelBindingProviderTests(unittest.TestCase):
  def test_config_model_binding_provider_resolves_agent_specific_model(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      ConfigCenterService(uow_factory).update_section(
        "llm",
        {
          "providers": [
            {
              "provider_id": "openai_primary",
              "kind": "openai-compatible",
              "base_url": "https://llm.example/v1",
              "api_key": "secret",
              "models": [{"model_id": "gpt-a", "enabled": True}],
            },
            {
              "provider_id": "gemini_primary",
              "kind": "gemini",
              "base_url": "https://gemini.example/v1beta",
              "api_key": "gemini-secret",
              "timeout_seconds": 12,
              "models": [{"model_id": "gemini-b", "enabled": True}],
            },
          ],
          "agent_bindings": [
            {"agent_id": "desktop_daily_agent", "provider_id": "gemini_primary", "model_id": "gemini-b"}
          ],
        },
        merge=False,
      )

      config = ConfigModelBindingProvider(uow_factory).load_config("desktop_daily_agent")

      self.assertEqual(config.provider, "gemini")
      self.assertEqual(config.model, "gemini-b")
      self.assertEqual(config.api_key, "gemini-secret")
      self.assertEqual(config.base_url, "https://gemini.example/v1beta")
      self.assertEqual(config.timeout_seconds, 12)
    finally:
      conn.close()

  def test_config_model_binding_provider_uses_default_config(self) -> None:
    conn = connect_sqlite()
    try:
      provider = ConfigModelBindingProvider(
        unit_of_work_factory(conn),
        default_llm_config=LLMConfig(
          provider="openai-compatible",
          model="default-model",
          api_key="default-key",
          base_url="https://llm.default/v1",
          timeout_seconds=33,
        ),
      )

      config = provider.load_config("desktop_daily_agent")

      self.assertEqual(config.model, "default-model")
      self.assertEqual(config.api_key, "default-key")
      self.assertEqual(config.base_url, "https://llm.default/v1")
      self.assertEqual(config.timeout_seconds, 33)
    finally:
      conn.close()


if __name__ == "__main__":
  unittest.main()
