#!/usr/bin/env python3
"""Compare Meadow Daily Agent and GenericAgent on the same prompt.

The script is intentionally outside runtime code. It provides repeatable E2E
evidence for browser/tool-loop behavior without hardcoding production flows.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import importlib
import json
import os
from pathlib import Path
import queue
import shutil
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_PROMPT = "通过浏览器打开小红书，刷新一下获取最新推荐帖子"
DEFAULT_TARGET_URL = "https://www.xiaohongshu.com/explore"
DEFAULT_TARGET_MATCH = "xiaohongshu.com,小红书,xhslink.com"


@dataclass(frozen=True, slots=True)
class BrowserPrepareReport:
  mode: str
  before: list[dict[str, Any]]
  after: list[dict[str, Any]]
  actions: list[dict[str, Any]]


def _json_default(value: object) -> str:
  return str(value)


def _utc_ts() -> str:
  return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, data: object) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")


def _append_jsonl(path: Path, data: object) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(data, ensure_ascii=False, default=_json_default) + "\n")


def _browser_link_post(base_url: str, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
  request = Request(
    base_url,
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
  )
  try:
    with urlopen(request, timeout=timeout) as response:
      raw = response.read().decode("utf-8", errors="replace")
  except URLError as exc:
    raise RuntimeError(f"Browser link unavailable at {base_url}: {exc}") from exc
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError as exc:
    raise RuntimeError(f"Browser link returned non-JSON response: {raw[:200]}") from exc
  if not isinstance(parsed, dict):
    raise RuntimeError(f"Browser link returned invalid response: {type(parsed).__name__}")
  return parsed


def _list_browser_tabs(base_url: str) -> list[dict[str, Any]]:
  response = _browser_link_post(base_url, {"cmd": "get_all_sessions"})
  tabs = response.get("r")
  if not isinstance(tabs, list):
    return []
  return [tab for tab in tabs if isinstance(tab, dict)]


def _execute_browser_js(
  base_url: str,
  *,
  code: str,
  session_id: str | None = None,
  timeout: float = 30.0,
) -> dict[str, Any]:
  response = _browser_link_post(
    base_url,
    {"cmd": "execute_js", "sessionId": session_id, "code": code, "timeout": str(timeout)},
    timeout=timeout + 5,
  )
  result = response.get("r", {})
  return result if isinstance(result, dict) else {"data": result}


def _matches_target_tab(tab: dict[str, Any], match_terms: list[str]) -> bool:
  url = str(tab.get("url") or "").lower()
  title = str(tab.get("title") or "").lower()
  return any(term and (term.lower() in url or term.lower() in title) for term in match_terms)


def prepare_browser_state(base_url: str, mode: str, target_url: str, match_terms: list[str]) -> BrowserPrepareReport:
  """Prepare comparable browser state before each agent run.

  Modes:
  - unchanged: record only.
  - target-open/xhs-open: ensure at least one active target tab exists.
  - target-closed/xhs-closed: move existing target tabs away from the site.

  The browser bridge does not expose tab removal, so "closed" means no visible
  scriptable target remains after navigating matching tabs away.
  """

  normalized_mode = {"xhs-open": "target-open", "xhs-closed": "target-closed"}.get(mode, mode)
  before = _list_browser_tabs(base_url)
  actions: list[dict[str, Any]] = []
  if normalized_mode == "unchanged":
    after = _list_browser_tabs(base_url)
    return BrowserPrepareReport(mode=mode, before=before, after=after, actions=actions)

  target_tabs = [tab for tab in before if _matches_target_tab(tab, match_terms)]
  if normalized_mode == "target-open":
    if target_tabs:
      tab_id = str(target_tabs[-1].get("id"))
      result = _execute_browser_js(
        base_url,
        session_id=tab_id,
        code="location.href = " + json.dumps(target_url) + ";",
        timeout=20,
      )
      actions.append({"action": "activate_existing_target", "target_id": tab_id, "result": result})
    else:
      command = {"cmd": "tabs", "method": "create", "url": target_url, "active": True}
      result = _execute_browser_js(base_url, code=json.dumps(command, ensure_ascii=False), timeout=20)
      actions.append({"action": "create_target_tab", "result": result})
    time.sleep(3)
  elif normalized_mode == "target-closed":
    for tab in target_tabs:
      tab_id = str(tab.get("id"))
      result = _execute_browser_js(
        base_url,
        session_id=tab_id,
        code="location.href = 'about:blank';",
        timeout=15,
      )
      actions.append({"action": "navigate_target_away", "target_id": tab_id, "result": result})
    time.sleep(1)
  else:
    raise ValueError(f"Unsupported browser prepare mode: {mode}")
  after = _list_browser_tabs(base_url)
  return BrowserPrepareReport(mode=mode, before=before, after=after, actions=actions)


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
  messages = payload.get("messages")
  if not isinstance(messages, list):
    messages = payload.get("input") if isinstance(payload.get("input"), list) else []
  summary: dict[str, Any] = {
    "model": payload.get("model"),
    "stream": payload.get("stream"),
    "message_count": len(messages),
    "tool_count": len(payload.get("tools") or []),
    "message_chars": sum(len(json.dumps(item, ensure_ascii=False, default=_json_default)) for item in messages),
    "message_roles": [item.get("role") for item in messages if isinstance(item, dict)],
  }
  if messages:
    last = messages[-1] if isinstance(messages[-1], dict) else {}
    summary["last_message_preview"] = str(last.get("content", ""))[:1200]
  return summary


def _tool_sequence_from_meadow(result: dict[str, Any]) -> list[dict[str, Any]]:
  daily = result.get("daily_agent")
  calls = daily.get("tool_calls") if isinstance(daily, dict) else None
  if not isinstance(calls, list):
    return []
  sequence: list[dict[str, Any]] = []
  for call in calls:
    if not isinstance(call, dict):
      continue
    output = call.get("output") if isinstance(call.get("output"), dict) else {}
    page = output.get("page") if isinstance(output.get("page"), dict) else {}
    sequence.append(
      {
        "name": call.get("name"),
        "capability_id": call.get("capability_id"),
        "ok": call.get("ok"),
        "target_id": output.get("target_id") or output.get("active_target_id") or call.get("input", {}).get("target_id"),
        "page_title": page.get("title"),
        "page_url": page.get("url"),
        "feed_titles": len(page.get("feed_titles") or []) if isinstance(page.get("feed_titles"), list) else 0,
        "search_results": len(page.get("search_results") or []) if isinstance(page.get("search_results"), list) else 0,
        "error": call.get("error"),
      }
    )
  return sequence


def _tool_sequence_from_generic_output(text: str) -> list[str]:
  return _dedupe_consecutive([line.strip() for line in text.splitlines() if line.startswith("🛠️")])


def _dedupe_consecutive(items: list[str]) -> list[str]:
  deduped: list[str] = []
  for item in items:
    if not deduped or deduped[-1] != item:
      deduped.append(item)
  return deduped


async def run_meadow(root: Path, db_path: str, out_dir: Path, prompt: str, max_turns: int) -> dict[str, Any]:
  sys.path.insert(0, str(root))
  from agent_kernel.agents import ContinuousRunnerConfig
  from agent_kernel.app.control_plane import ControlPlaneService
  from agent_kernel.app.daily_agent import ContinuousDailyAgentExecutor, DAILY_AGENT_ID, DailyAgentRequest
  from agent_kernel.app.model_binding import ConfigModelBindingProvider
  from agent_kernel.app.orchestration_tools import CompositeToolCatalog, OrchestrationCapabilityProvider
  from agent_kernel.autonomy.builtin_skills import ensure_builtin_atomic_skills
  from agent_kernel.autonomy.skill_service import SkillService
  from agent_kernel.capabilities import AtomicCapabilityProvider, CapabilityRegistry, CapabilityRuntime
  from agent_kernel.capabilities.adapters import LocalFileWorkspace, LocalToolExecutor, UrllibHTTPClient
  from agent_kernel.hosts.http import SampleWorkflowTaskLauncher, _default_desktop_atomic_grants
  from agent_kernel.memory import MemoryFacade
  from agent_kernel.models import ModelGateway, OpenAICompatibleProvider
  from agent_kernel.models.openai_compatible import _urllib_transport
  from agent_kernel.persistence import connect_sqlite
  from agent_kernel.policy import PolicyEngine
  from agent_kernel.runtime import unit_of_work_factory

  conn = connect_sqlite(db_path)
  try:
    uow_factory = unit_of_work_factory(conn)
    binding_provider = ConfigModelBindingProvider(uow_factory)
    config = binding_provider.load_config(DAILY_AGENT_ID)
    if config.provider.lower() not in {"openai-compatible", "openai", "deepseek", "new-api"}:
      raise RuntimeError(f"Comparison currently supports OpenAI-compatible providers only: {config.provider}")

    payloads_path = out_dir / "meadow_payloads.jsonl"

    def logging_transport(url: str, headers: dict[str, str], payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
      _append_jsonl(
        payloads_path,
        {
          "ts": _utc_ts(),
          "url": url,
          "payload": payload,
          "summary": _payload_summary(payload),
        },
      )
      return _urllib_transport(url, headers, payload, timeout_seconds)

    gateway = ModelGateway()
    gateway.register_provider(
      config.provider,
      OpenAICompatibleProvider(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        transport=logging_transport,
      ),
    )

    skill_service = SkillService(uow_factory)
    ensure_builtin_atomic_skills(skill_service)
    memory = MemoryFacade(uow_factory)
    atomic = AtomicCapabilityProvider(
      file_workspace=LocalFileWorkspace([str(root)]),
      http_client=UrllibHTTPClient(),
      memory=memory,
      uow_factory=uow_factory,
      skill_service=skill_service,
    )
    task_launcher = SampleWorkflowTaskLauncher(uow_factory)
    orchestration = OrchestrationCapabilityProvider(uow_factory=uow_factory, task_launcher=task_launcher)
    registry = CapabilityRegistry()
    local_tools = LocalToolExecutor()
    atomic.register(registry, local_tools)
    orchestration.register(registry, local_tools)
    runtime = CapabilityRuntime(
      registry,
      PolicyEngine(grants=_default_desktop_atomic_grants()),
      local_tools,
      control_workbench=ControlPlaneService.from_config(
        {
          "browser": {
            "enabled": True,
            "base_url": os.environ.get("MEADOW_BROWSER_LINK_URL", "http://127.0.0.1:18766/link"),
          },
          "desktop": {"enabled": False},
          "mobile": {"enabled": False},
        }
      ).workbench,
      uow_factory=uow_factory,
    )
    executor = ContinuousDailyAgentExecutor(
      uow_factory=uow_factory,
      skill_service=skill_service,
      capability_runtime=runtime,
      tool_catalog=CompositeToolCatalog([atomic, orchestration]),
      memory=memory,
    )
    run_id = f"compare_meadow_{int(time.time())}"
    response = await executor.execute(
      DailyAgentRequest(
        session_id=f"compare_session_{int(time.time())}",
        run_id=run_id,
        user_content=prompt,
        history_messages=[],
        selected_skill_ids=[],
        provider_name=config.provider,
        model_ref=config.model,
        agent_id=DAILY_AGENT_ID,
        max_turns=max_turns,
      ),
      gateway,
    )
    result = {"content": response.content, "raw": response.raw, "run_id": run_id}
    _write_json(out_dir / "meadow_result.json", result)
    _write_json(out_dir / "meadow_tool_sequence.json", _tool_sequence_from_meadow(response.raw))
    return result
  finally:
    conn.close()


def _load_daily_model_config(root: Path, db_path: str) -> Any:
  sys.path.insert(0, str(root))
  from agent_kernel.app.daily_agent import DAILY_AGENT_ID
  from agent_kernel.app.model_binding import ConfigModelBindingProvider
  from agent_kernel.persistence import connect_sqlite
  from agent_kernel.runtime import unit_of_work_factory

  conn = connect_sqlite(db_path)
  try:
    return ConfigModelBindingProvider(unit_of_work_factory(conn)).load_config(DAILY_AGENT_ID)
  finally:
    conn.close()


def _write_genericagent_temp_mykey(root: Path, db_path: str, temp_root: Path) -> Path:
  config = _load_daily_model_config(root, db_path)
  key_dir = temp_root / "genericagent_key"
  key_dir.mkdir(parents=True, exist_ok=True)
  mykey_path = key_dir / "mykey.py"
  mykey_path.write_text(
    "\n".join(
      [
        "# Auto-generated temporary config for comparison only.",
        "native_oai_api = {",
        "    'name': 'meadow-current',",
        f"    'apikey': {config.api_key!r},",
        f"    'apibase': {config.base_url!r},",
        f"    'model': {config.model!r},",
        "    'api_mode': 'chat_completions',",
        "    'stream': False,",
        f"    'read_timeout': {max(240, int(config.timeout_seconds))},",
        "}",
        "mixin_config = {'llm_nos': ['meadow-current'], 'max_retries': 0}",
        "",
      ]
    ),
    encoding="utf-8",
  )
  with contextlib.suppress(Exception):
    mykey_path.chmod(0o600)
  return key_dir


def run_generic(root: Path, db_path: str, out_dir: Path, prompt: str, timeout_seconds: int) -> dict[str, Any]:
  ga_root = root / "external_repos" / "GenericAgent"
  if not ga_root.exists():
    raise RuntimeError(f"GenericAgent repo not found: {ga_root}")
  payloads_path = out_dir / "generic_payloads.jsonl"
  browser_calls_path = out_dir / "generic_browser_calls.jsonl"

  with tempfile.TemporaryDirectory(prefix="meadow_genericagent_compare_") as temp_dir:
    temp_key_dir = _write_genericagent_temp_mykey(root, db_path, Path(temp_dir))
    sys.path.insert(0, str(temp_key_dir))
    sys.path.insert(0, str(ga_root))
    os.environ.setdefault("GA_LANG", "zh")

    llmcore = importlib.import_module("llmcore")
    original_post = llmcore.requests.post

    def logging_post(url, *args, **kwargs):
      payload = kwargs.get("json")
      if isinstance(payload, dict):
        event = {
          "ts": _utc_ts(),
          "url": url,
          "payload": payload,
          "summary": _payload_summary(payload),
        }
        if isinstance(payload.get("messages"), list) and payload.get("model"):
          _append_jsonl(payloads_path, event)
        elif payload.get("cmd"):
          _append_jsonl(browser_calls_path, event)
      return original_post(url, *args, **kwargs)

    llmcore.requests.post = logging_post
    try:
      agentmain = importlib.import_module("agentmain")
      agent = agentmain.GeneraticAgent()
      agent.next_llm(0)
      agent.verbose = False
      agent.peer_hint = False
      agent.force_non_stream = True
      worker = threading.Thread(target=agent.run, daemon=True)
      worker.start()
      dq = agent.put_task(prompt, source="compare")
      chunks: list[str] = []
      outputs: list[dict[str, Any]] = []
      deadline = time.time() + timeout_seconds
      done = None
      while time.time() < deadline:
        try:
          item = dq.get(timeout=1)
        except queue.Empty:
          continue
        outputs.append(item)
        if "next" in item:
          chunks.append(str(item["next"]))
        if "done" in item:
          done = item
          chunks.append(str(item["done"]))
          break
      if done is None:
        with contextlib.suppress(Exception):
          agent.abort()
        done = {"timeout": True, "message": f"GenericAgent timed out after {timeout_seconds}s"}
      output_text = "\n".join(chunks)
      (out_dir / "generic_output.txt").write_text(output_text, encoding="utf-8", errors="replace")
      _write_json(out_dir / "generic_queue_items.json", outputs)
      _write_json(
        out_dir / "generic_result.json",
        {
          "done": done,
          "history": getattr(agent, "history", []),
          "handler_history": getattr(getattr(agent, "handler", None), "history_info", []),
          "llm_log_path": getattr(agent, "log_path", None),
          "copied_llm_log_path": str(out_dir / "generic_model_responses.txt"),
        },
      )
      if getattr(agent, "log_path", None) and Path(agent.log_path).exists():
        target = out_dir / "generic_model_responses.txt"
        target.write_text(Path(agent.log_path).read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
      return {"done": done, "output_chars": len(output_text)}
    finally:
      llmcore.requests.post = original_post


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
  if not path.exists():
    return []
  return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_json(path: Path, default: Any) -> Any:
  if not path.exists():
    return default
  return json.loads(path.read_text(encoding="utf-8"))


def analyze_outputs(out_dir: Path, prompt: str) -> dict[str, Any]:
  meadow_payloads = [item for item in _load_jsonl(out_dir / "meadow_payloads.jsonl") if _is_llm_payload_event(item)]
  generic_payloads = [item for item in _load_jsonl(out_dir / "generic_payloads.jsonl") if _is_llm_payload_event(item)]
  generic_browser_calls = _load_jsonl(out_dir / "generic_browser_calls.jsonl")
  meadow_tools = _read_json(out_dir / "meadow_tool_sequence.json", [])
  generic_text = (out_dir / "generic_output.txt").read_text(encoding="utf-8", errors="replace") if (out_dir / "generic_output.txt").exists() else ""
  meadow_result = _read_json(out_dir / "meadow_result.json", {})
  generic_result = _read_json(out_dir / "generic_result.json", {})
  generic_tools = _generic_tool_sequence(generic_text, generic_result)
  meadow_feed_count = sum(item.get("feed_titles", 0) for item in meadow_tools if isinstance(item, dict))
  analysis = {
    "prompt": prompt,
    "request_counts": {
      "meadow": len(meadow_payloads),
      "generic": len(generic_payloads),
    },
    "tool_counts": {
      "meadow_actual_calls": len(meadow_tools),
      "generic_visible_tool_lines": len(generic_tools),
      "generic_browser_bridge_calls": len(generic_browser_calls),
      "meadow_exposed_tools_per_request": [item.get("summary", {}).get("tool_count") for item in meadow_payloads],
      "generic_exposed_tools_per_request": [item.get("summary", {}).get("tool_count") for item in generic_payloads],
    },
    "context_chars_per_request": {
      "meadow": [item.get("summary", {}).get("message_chars") for item in meadow_payloads],
      "generic": [item.get("summary", {}).get("message_chars") for item in generic_payloads],
    },
    "message_roles_per_request": {
      "meadow": [item.get("summary", {}).get("message_roles") for item in meadow_payloads],
      "generic": [item.get("summary", {}).get("message_roles") for item in generic_payloads],
    },
    "meadow_tool_sequence": meadow_tools,
    "generic_tool_sequence_lines": generic_tools,
    "final_outputs": {
      "meadow": str(meadow_result.get("content", ""))[:4000],
      "generic_done": generic_result.get("done"),
      "generic_output": generic_text[:4000],
    },
    "observations": _derive_observations(meadow_payloads, generic_payloads, meadow_tools, generic_tools, meadow_feed_count),
    "files": {
      "meadow_payloads": str(out_dir / "meadow_payloads.jsonl"),
      "meadow_result": str(out_dir / "meadow_result.json"),
      "meadow_tool_sequence": str(out_dir / "meadow_tool_sequence.json"),
      "generic_payloads": str(out_dir / "generic_payloads.jsonl"),
      "generic_browser_calls": str(out_dir / "generic_browser_calls.jsonl"),
      "generic_output": str(out_dir / "generic_output.txt"),
      "generic_result": str(out_dir / "generic_result.json"),
      "generic_model_responses": str(out_dir / "generic_model_responses.txt"),
    },
  }
  _write_json(out_dir / "analysis_summary.json", analysis)
  return analysis


def _is_llm_payload_event(event: dict[str, Any]) -> bool:
  payload = event.get("payload")
  return isinstance(payload, dict) and isinstance(payload.get("messages"), list) and bool(payload.get("model"))


def _generic_tool_sequence(generic_text: str, generic_result: dict[str, Any]) -> list[str]:
  log_path = generic_result.get("llm_log_path")
  if isinstance(log_path, str) and Path(log_path).exists():
    text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    parsed = _tool_sequence_from_model_response_log(text)
    if parsed:
      return parsed
  copied_log = Path(generic_result.get("copied_llm_log_path", "")) if isinstance(generic_result.get("copied_llm_log_path"), str) else None
  if copied_log and copied_log.exists():
    parsed = _tool_sequence_from_model_response_log(copied_log.read_text(encoding="utf-8", errors="replace"))
    if parsed:
      return parsed
  return _tool_sequence_from_generic_output(generic_text)


def _tool_sequence_from_model_response_log(text: str) -> list[str]:
  tools: list[str] = []
  for line in text.splitlines():
    stripped = line.strip()
    if "'type': 'tool_use'" not in stripped and '"type": "tool_use"' not in stripped:
      continue
    # The log is Python repr-like. Keep extraction conservative and
    # human-readable instead of evaluating arbitrary text.
    name = _extract_between(stripped, "'name': '", "'") or _extract_between(stripped, '"name": "', '"')
    input_text = _extract_between(stripped, "'input': ", "}]") or _extract_between(stripped, '"input": ', "}]")
    if name:
      tools.append(f"{name}({input_text or '{}'})")
  return tools


def _extract_between(text: str, prefix: str, suffix: str) -> str | None:
  start = text.find(prefix)
  if start < 0:
    return None
  start += len(prefix)
  end = text.find(suffix, start)
  if end < 0:
    return text[start:]
  return text[start:end]


def _derive_observations(
  meadow_payloads: list[dict[str, Any]],
  generic_payloads: list[dict[str, Any]],
  meadow_tools: list[dict[str, Any]],
  generic_tools: list[str],
  meadow_feed_count: int,
) -> list[str]:
  observations: list[str] = []
  meadow_tool_counts = [item.get("summary", {}).get("tool_count") for item in meadow_payloads]
  generic_tool_counts = [item.get("summary", {}).get("tool_count") for item in generic_payloads]
  if meadow_tool_counts and generic_tool_counts:
    observations.append(f"Meadow exposes {meadow_tool_counts[0]} tools on the first request; GenericAgent exposes {generic_tool_counts[0]}.")
  if len(meadow_payloads) and len(generic_payloads):
    observations.append(f"Meadow used {len(meadow_payloads)} LLM requests; GenericAgent used {len(generic_payloads)}.")
  if meadow_feed_count:
    observations.append(f"Meadow browser_scan observations contained {meadow_feed_count} feed title entries.")
  failed = [item for item in meadow_tools if isinstance(item, dict) and not item.get("ok")]
  if failed:
    observations.append("Meadow had failed tool calls: " + "；".join(str(item.get("name")) for item in failed[-5:]))
  if generic_tools:
    observations.append("GenericAgent tool trace: " + " -> ".join(generic_tools[:8]))
  return observations


def run_comparison(args: argparse.Namespace, run_name: str, *, skip_meadow: bool, skip_generic: bool) -> dict[str, Any]:
  root = Path(args.root).resolve()
  out_dir = Path(args.out_dir).resolve() / run_name
  if out_dir.exists() and args.clean:
    shutil.rmtree(out_dir)
  out_dir.mkdir(parents=True, exist_ok=True)
  target_url = args.target_url or args.xhs_url
  match_terms = _parse_match_terms(args.target_match)

  prepare_reports: dict[str, Any] = {}
  if args.browser_state != "unchanged":
    if not skip_meadow:
      report = prepare_browser_state(args.browser_link_url, args.browser_state, target_url, match_terms)
      prepare_reports["before_meadow"] = asdict(report)
      _write_json(out_dir / "browser_prepare_before_meadow.json", asdict(report))
    if not skip_generic:
      report = prepare_browser_state(args.browser_link_url, args.browser_state, target_url, match_terms)
      prepare_reports["before_generic"] = asdict(report)
      _write_json(out_dir / "browser_prepare_before_generic.json", asdict(report))
  else:
    report = prepare_browser_state(args.browser_link_url, "unchanged", target_url, match_terms)
    prepare_reports["initial"] = asdict(report)
    _write_json(out_dir / "browser_prepare_initial.json", asdict(report))

  if not skip_meadow:
    asyncio.run(run_meadow(root, args.db, out_dir, args.prompt, args.meadow_max_turns))
  if not skip_generic:
    run_generic(root, args.db, out_dir, args.prompt, args.generic_timeout)
  analysis = analyze_outputs(out_dir, args.prompt)
  analysis["browser_prepare"] = prepare_reports
  _write_json(out_dir / "analysis_summary.json", analysis)
  return analysis


def _parse_match_terms(value: str) -> list[str]:
  return [term.strip() for term in value.split(",") if term.strip()]


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
  parser.add_argument("--db", default="/tmp/meadow-desktop.sqlite")
  parser.add_argument("--out-dir", default="/tmp/agent_compare_runs")
  parser.add_argument("--prompt", default=DEFAULT_PROMPT)
  parser.add_argument("--browser-link-url", default=os.environ.get("MEADOW_BROWSER_LINK_URL", "http://127.0.0.1:18766/link"))
  parser.add_argument(
    "--browser-state",
    choices=["unchanged", "target-open", "target-closed", "xhs-open", "xhs-closed"],
    default="target-open",
  )
  parser.add_argument("--target-url", default=DEFAULT_TARGET_URL)
  parser.add_argument("--target-match", default=DEFAULT_TARGET_MATCH, help="Comma-separated URL/title terms used by browser-state prep.")
  parser.add_argument("--xhs-url", default=DEFAULT_TARGET_URL, help=argparse.SUPPRESS)
  parser.add_argument("--meadow-max-turns", type=int, default=16)
  parser.add_argument("--generic-timeout", type=int, default=420)
  parser.add_argument("--skip-meadow", action="store_true")
  parser.add_argument("--skip-generic", action="store_true")
  parser.add_argument("--clean", action="store_true")
  parser.add_argument("--run-name", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
  args = parser.parse_args()

  analysis = run_comparison(args, args.run_name, skip_meadow=args.skip_meadow, skip_generic=args.skip_generic)
  print(json.dumps(analysis, ensure_ascii=False, indent=2, default=_json_default))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
