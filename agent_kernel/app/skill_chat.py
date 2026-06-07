"""Skill-planned chat execution for desktop conversations."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from agent_kernel.capabilities import AtomicToolCatalog, CapabilityCallContext, CapabilityRuntime
from agent_kernel.domain.context import ModelContext
from agent_kernel.domain.skill import SkillCard
from agent_kernel.models import ModelGateway


@dataclass(slots=True)
class PlannedSkillToolCall:
  name: str
  input: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SkillPlan:
  mode: str
  skill_id: str | None = None
  tool_calls: list[PlannedSkillToolCall] = field(default_factory=list)
  response: str | None = None
  final_response_instruction: str | None = None
  raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SkillChatOutcome:
  content: str
  planning_result: dict[str, Any]
  tool_results: list[dict[str, Any]] = field(default_factory=list)
  final_result: dict[str, Any] | None = None
  error: dict[str, Any] | None = None

  def to_dict(self) -> dict[str, Any]:
    data: dict[str, Any] = {
      "content": self.content,
      "planning_result": self.planning_result,
      "tool_results": self.tool_results,
    }
    if self.final_result is not None:
      data["final_result"] = self.final_result
    if self.error is not None:
      data["error"] = self.error
    return data


class SkillChatExecutor:
  """Lets the model choose a Skill, executes approved atomic tools, then asks for a final answer."""

  def __init__(
    self,
    *,
    model_gateway: ModelGateway,
    provider_name: str,
    model_ref: str,
    capability_runtime: CapabilityRuntime,
    tool_catalog: AtomicToolCatalog,
    skills: list[SkillCard],
  ) -> None:
    self._model_gateway = model_gateway
    self._provider_name = provider_name
    self._model_ref = model_ref
    self._capability_runtime = capability_runtime
    self._tool_catalog = tool_catalog
    self._skills = skills

  async def complete(
    self,
    *,
    run_id: str,
    session_id: str,
    user_content: str,
    history: list[dict[str, str]],
  ) -> SkillChatOutcome:
    planning_messages = self._planning_messages(session_id, user_content, history)
    planning_result = await self._model_gateway.complete(
      self._provider_name,
      self._model_ref,
      ModelContext(messages=planning_messages),
    )
    plan = parse_skill_plan(planning_result.get("content"))
    if plan is None:
      content = _content_or_empty(planning_result)
      return SkillChatOutcome(content=content or "LLM 返回了空内容。", planning_result=planning_result)
    if plan.mode in {"respond", "direct", "chat"} or not plan.tool_calls:
      content = plan.response or plan.final_response_instruction or _content_or_empty(planning_result)
      return SkillChatOutcome(content=content or "LLM 未选择工具调用，也未返回可展示内容。", planning_result=planning_result)

    selected_skill = self._skill_by_id(plan.skill_id)
    allowed_tools = set(selected_skill.recommended_tools if selected_skill is not None else [])
    if not allowed_tools:
      return SkillChatOutcome(
        content="模型选择了 Skill，但该 Skill 没有配置可用原子工具。",
        planning_result=planning_result,
        error={"type": "skill_has_no_tools", "skill_id": plan.skill_id},
      )

    tool_results: list[dict[str, Any]] = []
    for tool_call in plan.tool_calls[:6]:
      if tool_call.name not in allowed_tools:
        tool_results.append(
          {
            "name": tool_call.name,
            "ok": False,
            "error": {
              "type": "tool_not_allowed_by_skill",
              "message": f"Tool {tool_call.name} is not allowed by Skill {selected_skill.skill_id}.",
            },
          }
        )
        continue
      normalized = self._tool_catalog.normalize_call(
        tool_call.name,
        tool_call.input,
        run_id=run_id,
        scope=session_id,
      )
      outcome = await self._capability_runtime.call(
        normalized.capability_id,
        normalized.input,
        CapabilityCallContext(run_id=run_id, agent_id="desktop_chat", task_id=session_id),
      )
      tool_results.append(
        {
          "name": normalized.display_name,
          "capability_id": normalized.capability_id,
          "input": normalized.input,
          "decision": {
            "type": outcome.decision.type.value,
            "reason": outcome.decision.reason,
          },
          "result": outcome.result.envelope() if outcome.result is not None else None,
        }
      )

    final_messages = self._final_messages(user_content, history, selected_skill, plan, tool_results)
    try:
      final_result = await self._model_gateway.complete(
        self._provider_name,
        self._model_ref,
        ModelContext(messages=final_messages),
      )
      content = _content_or_empty(final_result)
    except Exception as exc:
      final_result = {"error": {"type": "llm_call_failed", "message": str(exc)}}
      content = ""
    if not content:
      content = _fallback_tool_summary(tool_results)
    return SkillChatOutcome(
      content=content,
      planning_result=planning_result,
      tool_results=tool_results,
      final_result=final_result,
    )

  def _planning_messages(
    self,
    session_id: str,
    user_content: str,
    history: list[dict[str, str]],
  ) -> list[dict[str, Any]]:
    return [
      {
        "role": "system",
        "content": (
          "你是 Meadow 日常 Agent 的 Skill 调度器。根据用户目标判断是否需要调用 Skill。"
          "如果需要调用能力，必须返回严格 JSON，格式为："
          '{"mode":"use_skill","skill_id":"...","tool_calls":[{"name":"工具名","input":{}}],'
          '"final_response_instruction":"如何基于工具结果回答"}。'
          "如果不需要工具，返回普通自然语言或 JSON："
          '{"mode":"respond","response":"..."}。'
          "不要伪造工具结果；需要实时信息、网页、浏览器、文件、代码执行、桌面/移动控制、子 Agent 或记忆埋点时选择 Skill。"
        ),
      },
      {"role": "system", "content": "可用 Skills:\n" + json.dumps(_skill_prompt(self._skills), ensure_ascii=False)},
      {"role": "system", "content": "可用原子工具 schema:\n" + json.dumps(self._tool_catalog.tool_schemas(), ensure_ascii=False)},
      {"role": "system", "content": f"当前 chat session: {session_id}"},
      *history,
      {"role": "user", "content": user_content},
    ]

  def _final_messages(
    self,
    user_content: str,
    history: list[dict[str, str]],
    selected_skill: SkillCard | None,
    plan: SkillPlan,
    tool_results: list[dict[str, Any]],
  ) -> list[dict[str, Any]]:
    return [
      {
        "role": "system",
        "content": (
          "你是 Meadow 日常 Agent。请用中文基于工具真实结果回答用户。"
          "不要声称已经做了未发生的动作；如果工具失败，简洁说明失败原因和下一步。"
        ),
      },
      *history,
      {"role": "user", "content": user_content},
      {
        "role": "system",
        "content": json.dumps(
          {
            "selected_skill": selected_skill.to_dict() if selected_skill is not None else None,
            "plan": plan.raw,
            "tool_results": tool_results,
            "final_response_instruction": plan.final_response_instruction,
          },
          ensure_ascii=False,
        ),
      },
    ]

  def _skill_by_id(self, skill_id: str | None) -> SkillCard | None:
    if skill_id is None:
      return None
    for skill in self._skills:
      if skill.skill_id == skill_id:
        return skill
    return None


def parse_skill_plan(value: object) -> SkillPlan | None:
  if not isinstance(value, str) or not value.strip():
    return None
  parsed = _loads_json_object(value)
  if parsed is None:
    return None
  mode = str(parsed.get("mode") or "")
  raw_calls = parsed.get("tool_calls", [])
  calls: list[PlannedSkillToolCall] = []
  if isinstance(raw_calls, list):
    for raw in raw_calls:
      if not isinstance(raw, dict):
        continue
      name = raw.get("name") or raw.get("tool") or raw.get("function")
      input_value = raw.get("input") if isinstance(raw.get("input"), dict) else raw.get("arguments")
      if isinstance(input_value, str):
        input_value = _loads_json_object(input_value) or {}
      if isinstance(name, str) and name:
        calls.append(PlannedSkillToolCall(name=name, input=input_value if isinstance(input_value, dict) else {}))
  return SkillPlan(
    mode=mode or ("use_skill" if calls else "respond"),
    skill_id=str(parsed["skill_id"]) if isinstance(parsed.get("skill_id"), str) else None,
    tool_calls=calls,
    response=str(parsed["response"]) if isinstance(parsed.get("response"), str) else None,
    final_response_instruction=str(parsed["final_response_instruction"])
    if isinstance(parsed.get("final_response_instruction"), str)
    else None,
    raw=parsed,
  )


def _loads_json_object(text: str) -> dict[str, Any] | None:
  stripped = text.strip()
  if stripped.startswith("```"):
    stripped = stripped.removeprefix("```json").removeprefix("```").strip()
    stripped = stripped.removesuffix("```").strip()
  start = stripped.find("{")
  end = stripped.rfind("}")
  if start < 0 or end < start:
    return None
  try:
    value = json.loads(stripped[start : end + 1])
  except json.JSONDecodeError:
    return None
  return value if isinstance(value, dict) else None


def _skill_prompt(skills: list[SkillCard]) -> list[dict[str, Any]]:
  return [
    {
      "skill_id": skill.skill_id,
      "name": skill.name,
      "description": skill.description,
      "when_to_use": skill.when_to_use,
      "instructions": skill.instructions,
      "recommended_tools": skill.recommended_tools,
      "constraints": skill.constraints,
      "failure_modes": skill.failure_modes,
    }
    for skill in skills
  ]


def _content_or_empty(result: dict[str, Any] | dict[str, object]) -> str:
  content = result.get("content")
  return content.strip() if isinstance(content, str) else ""


def _fallback_tool_summary(tool_results: list[dict[str, Any]]) -> str:
  if not tool_results:
    return "没有可用工具结果。"
  lines = ["已执行 Skill 原子工具，结果如下："]
  for item in tool_results:
    result = item.get("result")
    if isinstance(result, dict) and result.get("ok"):
      lines.append(f"- {item.get('name')}: 成功，输出 {json.dumps(result.get('output', {}), ensure_ascii=False)[:800]}")
    else:
      error = result.get("error") if isinstance(result, dict) else item.get("error")
      lines.append(f"- {item.get('name')}: 失败，原因 {json.dumps(error, ensure_ascii=False)}")
  return "\n".join(lines)
