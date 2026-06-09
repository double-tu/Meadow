from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from agent_kernel.capabilities import (
  AtomicCapabilityProvider,
  AtomicCapabilityIds,
  CapabilityCallContext,
  CapabilityRegistry,
  CapabilityRuntime,
)
from agent_kernel.capabilities.adapters import (
  ControlResult,
  ControlTarget,
  ControlWorkbench,
  FakeControlBackend,
  HTTPResponse,
  LocalFileWorkspace,
  LocalToolExecutor,
)
from agent_kernel.agents import AgentDelegationBroker, ConnectorTurn, FakeAgentConnector
from agent_kernel.autonomy import SkillService
from agent_kernel.domain import ArtifactRef, CapabilityGrant, RuntimeEvent, RuntimeEventType, SkillResource
from agent_kernel.domain.base import utc_now
from agent_kernel.memory import MemoryFacade
from agent_kernel.persistence import UnitOfWork, connect_sqlite
from agent_kernel.runtime import unit_of_work_factory
from agent_kernel.policy import PolicyEngine


class AtomicCapabilityTests(unittest.IsolatedAsyncioTestCase):
  async def test_browser_tool_schemas_guide_dynamic_page_extraction(self) -> None:
    provider = AtomicCapabilityProvider()
    schemas = {schema["function"]["name"]: schema["function"] for schema in provider.tool_schemas()}

    self.assertIn("dynamic feed/card pages", schemas["browser_scan"]["description"])
    self.assertIn("structured visible cards", schemas["browser_scan"]["description"])
    self.assertIn("visible cards", schemas["browser_execute_js"]["description"])
    self.assertIn("title/text/url/author/time/metrics", schemas["browser_execute_js"]["description"])

  async def test_workspace_read_supports_keyword_context_and_line_numbers(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / "notes.txt"
      path.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
      provider = AtomicCapabilityProvider(file_workspace=LocalFileWorkspace([tmp]))

      result = provider.read_workspace(
        {
          "path": str(path),
          "keyword": "gamma",
          "count": 2,
          "show_line_numbers": True,
        }
      )

      self.assertTrue(result.ok)
      self.assertEqual(result.output["content"], "2|beta\n3|gamma")
      self.assertEqual(result.output["start"], 2)
      self.assertTrue(result.output["matched"])

  async def test_http_request_uses_network_policy_and_adapter(self) -> None:
    registry = CapabilityRegistry()
    local_tools = LocalToolExecutor()
    client = _RecordingHTTPClient()
    provider = AtomicCapabilityProvider(http_client=client)
    provider.register(registry, local_tools)
    runtime = CapabilityRuntime(
      registry,
      PolicyEngine(
        grants=[
          CapabilityGrant(
            grant_id="grant_http",
            capability_id=AtomicCapabilityIds.HTTP_REQUEST,
            run_id="run_http",
            expires_at=utc_now() + timedelta(minutes=5),
            network_scope=["api.example.test"],
          )
        ]
      ),
      local_tools,
    )

    allowed = await runtime.call(
      AtomicCapabilityIds.HTTP_REQUEST,
      {"url": "https://api.example.test/status", "method": "GET"},
      CapabilityCallContext(run_id="run_http"),
    )
    denied = await runtime.call(
      AtomicCapabilityIds.HTTP_REQUEST,
      {"url": "https://other.example.test/status", "method": "GET"},
      CapabilityCallContext(run_id="run_http"),
    )

    self.assertTrue(allowed.result.ok)
    self.assertEqual(allowed.result.output["body"], "ok")
    self.assertEqual(client.requests[0][1], "https://api.example.test/status")
    self.assertFalse(denied.result.ok)
    self.assertEqual(denied.result.error["type"], "policy_denied")
    self.assertEqual(len(client.requests), 1)

  async def test_http_request_summarizes_html_feed_titles(self) -> None:
    client = _RecordingHTTPClient(
      body=(
        '<html><head><title>小红书 - 你的生活兴趣社区</title></head>'
        '<body><script>{"displayTitle":"云南大理避暑很舒服","displayTitle":"当了三十年的班主任"}</script></body></html>'
      )
    )
    provider = AtomicCapabilityProvider(http_client=client)

    result = provider.http_request({"url": "https://www.xiaohongshu.com/explore"})

    self.assertTrue(result.ok)
    self.assertEqual(result.output["body_summary"]["page_title"], "小红书 - 你的生活兴趣社区")
    self.assertEqual(
      result.output["body_summary"]["feed_titles"],
      ["云南大理避暑很舒服", "当了三十年的班主任"],
    )

  async def test_http_request_marks_anti_spider_page(self) -> None:
    client = _RecordingHTTPClient(
      body="<html><head><title>Sogou Antispider</title></head><body>antispider verify</body></html>"
    )
    provider = AtomicCapabilityProvider(http_client=client)

    result = provider.http_request({"url": "http://www.sogou.com/antispider/?m=1"})

    self.assertTrue(result.ok)
    self.assertEqual(result.output["access_issue"]["type"], "anti_spider_challenge")

  async def test_atomic_desktop_and_mobile_actions_route_to_control_workbench(self) -> None:
    registry = CapabilityRegistry()
    local_tools = LocalToolExecutor()
    provider = AtomicCapabilityProvider()
    provider.register(registry, local_tools)
    backend = FakeControlBackend()
    backend.register_target(ControlTarget(target_id="window_1", kind="desktop"))
    backend.register_target(ControlTarget(target_id="device_1", kind="mobile"))
    backend.register_response("desktop", "click", ControlResult(ok=True, output={"clicked": True}))
    backend.register_response("mobile", "dump_ui", ControlResult(ok=True, output={"nodes": [{"text": "Pay"}]}))
    runtime = CapabilityRuntime(
      registry,
      PolicyEngine(
        grants=[
          CapabilityGrant(
            grant_id="grant_desktop",
            capability_id=AtomicCapabilityIds.DESKTOP_CLICK,
            run_id="run_control",
            expires_at=utc_now() + timedelta(minutes=5),
          ),
          CapabilityGrant(
            grant_id="grant_mobile",
            capability_id=AtomicCapabilityIds.MOBILE_DUMP_UI,
            run_id="run_control",
            expires_at=utc_now() + timedelta(minutes=5),
          ),
        ]
      ),
      local_tools,
      control_workbench=ControlWorkbench(backend),
    )

    desktop_call = provider.normalize_call(
      "desktop_click",
      {"target_id": "window_1", "x": 10, "y": 20},
      run_id="run_control",
      scope="scope",
    )
    mobile_call = provider.normalize_call(
      "mobile_dump_ui",
      {"target_id": "device_1"},
      run_id="run_control",
      scope="scope",
    )
    desktop = await runtime.call(desktop_call.capability_id, desktop_call.input, CapabilityCallContext("run_control"))
    mobile = await runtime.call(mobile_call.capability_id, mobile_call.input, CapabilityCallContext("run_control"))

    self.assertTrue(desktop.result.ok)
    self.assertTrue(mobile.result.ok)
    self.assertEqual(backend.commands[0].target_kind, "desktop")
    self.assertEqual(backend.commands[0].action, "click")
    self.assertEqual(backend.commands[0].payload, {"x": 10, "y": 20})
    self.assertEqual(backend.commands[1].target_kind, "mobile")
    self.assertEqual(backend.commands[1].action, "dump_ui")
    self.assertEqual(mobile.result.output["nodes"][0]["text"], "Pay")

  async def test_model_visible_schema_exposes_default_atomic_surface(self) -> None:
    provider = AtomicCapabilityProvider()

    names = {schema["function"]["name"] for schema in provider.tool_schemas()}
    schemas = {schema["function"]["name"]: schema["function"] for schema in provider.tool_schemas()}

    self.assertIn("http_request", names)
    self.assertIn("desktop_click", names)
    self.assertIn("mobile_dump_ui", names)
    self.assertIn("memory_evolution_note", names)
    self.assertIn("agent_delegate", names)
    self.assertIn("agent_delegation_status", names)
    self.assertIn("agent_cancel_delegation", names)
    self.assertIn("skill_open", names)
    self.assertIn("skill_resource_open", names)
    self.assertIn("memory_search", names)
    self.assertIn("memory_read", names)
    self.assertIn("artifact_read", names)
    self.assertIn("event_search", names)
    self.assertIn("context_compact", names)
    self.assertIn("context_expand", names)
    self.assertIn("evidence_summary", schemas["memory_evolution_note"]["parameters"]["required"])

  async def test_memory_evolution_note_requires_evidence_summary(self) -> None:
    provider = AtomicCapabilityProvider()

    result = provider.record_memory_evolution_note({"note": "Maybe remember this."})

    self.assertFalse(result.ok)
    self.assertEqual(result.error["type"], "invalid_input")

  async def test_skill_resource_open_reads_bounded_sop_resource(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      skills = SkillService(uow_factory)
      resource = SkillResource(
        resource_id="res_browser_sop",
        skill_id="skill_browser",
        title="Browser SOP",
        content="step " * 100,
      )
      skills.save_resource(resource)
      provider = AtomicCapabilityProvider(skill_service=skills)

      opened = provider.open_skill_resource({"resource_id": "res_browser_sop", "max_chars": 20})

      self.assertTrue(opened.ok)
      self.assertEqual(opened.output["resource"]["resource_id"], "res_browser_sop")
      self.assertTrue(opened.output["resource"]["truncated"])
      self.assertTrue(opened.output["resource"]["content"].endswith("...[truncated]"))
    finally:
      conn.close()

  async def test_context_reader_tools_open_search_read_and_expand_refs(self) -> None:
    conn = connect_sqlite()
    try:
      uow_factory = unit_of_work_factory(conn)
      skills = SkillService(uow_factory)
      skill = skills.create_interpreted_skill(
        name="Browser inspect",
        description="Inspect pages with browser tools.",
        when_to_use="When the user asks to inspect a web page.",
        instructions="Use browser_scan before summarizing page content.",
        recommended_tools=["browser_scan"],
      )
      memory = MemoryFacade(uow_factory)
      working = memory.write_working("chat_1", {"key_info": "深圳 weather request"}, importance=0.9)
      semantic = memory.write_semantic("chat_1", {"summary": "User prefers Chinese output"}, importance=0.8)
      artifact = ArtifactRef("art_weather", "artifact://weather", "application/json")
      with UnitOfWork(conn) as uow:
        uow.artifacts.save(artifact, {"summary": "weather payload"})
        uow.events.append(
          RuntimeEvent(
            event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
            run_id="run_reader",
            payload={"summary": "browser_scan returned Shenzhen weather"},
            artifact_refs=[artifact],
          )
        )
      provider = AtomicCapabilityProvider(
        memory=memory,
        uow_factory=uow_factory,
        skill_service=skills,
      )

      opened = provider.open_skill({"skill_id": skill.skill_id})
      searched = provider.search_memory({"scope": "chat_1", "query": "weather", "limit": 5})
      read = provider.read_memory({"memory_id": working.memory_id})
      artifact_read = provider.read_artifact({"artifact_id": artifact.artifact_id})
      events = provider.search_events({"run_id": "run_reader", "query": "weather"})
      compacted = provider.compact_context(
        {
          "scope": "chat_1",
          "summary": "Older browser weather turn was compacted.",
          "artifact_ids": [artifact.artifact_id],
        }
      )
      expanded = provider.expand_context(
        {
          "memory_ids": [semantic.memory_id],
          "artifact_ids": [artifact.artifact_id],
          "skill_ids": [skill.skill_id],
        }
      )

      self.assertTrue(opened.ok)
      self.assertIn("Use browser_scan", opened.output["skill"]["instructions"])
      self.assertTrue(searched.ok)
      self.assertEqual(searched.output["results"][0]["memory_id"], working.memory_id)
      self.assertTrue(read.ok)
      self.assertEqual(read.output["memory"]["content"]["key_info"], "深圳 weather request")
      self.assertEqual(artifact_read.output["metadata"]["summary"], "weather payload")
      self.assertEqual(events.output["events"][0]["event_type"], RuntimeEventType.TOOL_CALL_COMPLETED.value)
      self.assertTrue(compacted.ok)
      self.assertEqual(compacted.output["memory_type"], "episodic")
      self.assertEqual(expanded.output["expanded"]["memories"][0]["memory_id"], semantic.memory_id)
      self.assertEqual(expanded.output["expanded"]["artifacts"][0]["artifact"]["artifact_id"], artifact.artifact_id)
      self.assertEqual(expanded.output["expanded"]["skills"][0]["skill_id"], skill.skill_id)
    finally:
      conn.close()

  async def test_artifact_read_can_return_bounded_metadata_and_file_content(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      conn = connect_sqlite()
      try:
        uow_factory = unit_of_work_factory(conn)
        file_path = Path(tmp) / "artifact.txt"
        file_path.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
        inline = ArtifactRef("art_inline", "artifact://inline", "text/plain")
        file_ref = ArtifactRef("art_file", f"file://{file_path}", "text/plain")
        with UnitOfWork(conn) as uow:
          uow.artifacts.save(inline, {"content": "one\ntwo\nthree"})
          uow.artifacts.save(file_ref, {"kind": "file"})
        provider = AtomicCapabilityProvider(
          file_workspace=LocalFileWorkspace([tmp]),
          uow_factory=uow_factory,
        )

        inline_read = provider.read_artifact({"artifact_id": inline.artifact_id, "start": 2, "count": 1})
        file_read = provider.read_artifact({"artifact_id": file_ref.artifact_id, "keyword": "gamma", "count": 2})

        self.assertTrue(inline_read.output["content_available"])
        self.assertEqual(inline_read.output["content"]["content"], "2|two")
        self.assertEqual(file_read.output["content_source"], "file_workspace")
        self.assertEqual(file_read.output["content"]["content"], "2|beta\n3|gamma")
      finally:
        conn.close()

  async def test_atomic_agent_delegation_routes_through_capability_runtime(self) -> None:
    conn = connect_sqlite()
    try:
      connector = FakeAgentConnector()
      connector.queue_response(
        ConnectorTurn(
          turn_id="turn_atomic_delegate",
          session_id="unused",
          output={"summary": "delegated"},
          completed=True,
        )
      )
      broker = AgentDelegationBroker(unit_of_work_factory(conn), {"codex": connector})
      registry = CapabilityRegistry()
      local_tools = LocalToolExecutor()
      provider = AtomicCapabilityProvider(delegation=broker)
      provider.register(registry, local_tools)
      runtime = CapabilityRuntime(
        registry,
        PolicyEngine(
          grants=[
            CapabilityGrant(
              grant_id="grant_delegate",
              capability_id=AtomicCapabilityIds.AGENT_DELEGATE,
              run_id="run_atomic_delegate",
              expires_at=utc_now() + timedelta(minutes=5),
            )
          ]
        ),
        local_tools,
      )

      delegated = await runtime.call(
        AtomicCapabilityIds.AGENT_DELEGATE,
        {
          "parent_run_id": "run_atomic_delegate",
          "connector_id": "codex",
          "agent_type": "implementation",
          "task": "implement capability",
        },
        CapabilityCallContext(run_id="run_atomic_delegate"),
      )
      task_id = delegated.result.output["delegation"]["task_id"]
      status = await runtime.call(
        AtomicCapabilityIds.AGENT_DELEGATION_STATUS,
        {
          "parent_run_id": "run_atomic_delegate",
          "task_ids": [task_id],
          "wait_ms": 500,
        },
        CapabilityCallContext(run_id="run_atomic_delegate"),
      )

      self.assertTrue(delegated.result.ok)
      self.assertTrue(status.result.ok)
      self.assertEqual(status.result.output["delegations"][0]["status"], "completed")
      self.assertEqual(status.result.output["delegations"][0]["output"], {"summary": "delegated"})
    finally:
      conn.close()


class _RecordingHTTPClient:
  def __init__(self, body: str = "ok") -> None:
    self.requests = []
    self.body = body

  def request(self, method, url, *, headers=None, body=None, timeout_seconds=None):
    self.requests.append((method, url, headers or {}, body, timeout_seconds))
    return HTTPResponse(status=200, headers={"content-type": "text/plain"}, body=self.body, url=url)


if __name__ == "__main__":
  unittest.main()
