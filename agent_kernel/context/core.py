"""Core agent context and capability navigation.

This provider gives every Meadow agent a compact operating constitution and a
minimal capability navigation index. It intentionally exposes existence and
routing hints, not long SOP bodies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_kernel.context.token_counter import estimate_tokens
from agent_kernel.domain.base import new_id
from agent_kernel.domain.context import ContextAssemblyRequest, ContextLayer, ContextLayerItem, ContextLayerKind


@dataclass(frozen=True, slots=True)
class CapabilityNavigationEntry:
  topic: str
  skill_ids: tuple[str, ...] = ()
  tools: tuple[str, ...] = ()
  resources: tuple[str, ...] = ()
  triggers: tuple[str, ...] = ()

  def to_context(self) -> dict[str, Any]:
    return {
      "topic": self.topic,
      "triggers": list(self.triggers),
      "skill_ids": list(self.skill_ids),
      "tools": list(self.tools),
      "resources": list(self.resources),
    }


@dataclass(frozen=True, slots=True)
class CoreAgentContext:
  constitution: tuple[str, ...]
  failure_escalation: tuple[str, ...]
  progressive_disclosure: tuple[str, ...]
  working_memory_rules: tuple[str, ...]
  memory_governance: tuple[str, ...]
  capability_navigation: tuple[CapabilityNavigationEntry, ...] = field(default_factory=tuple)

  def to_context(self) -> dict[str, Any]:
    return {
      "type": "core_agent_context",
      "purpose": "Compact operating constitution and capability navigation for Meadow agents.",
      "constitution": list(self.constitution),
      "failure_escalation": list(self.failure_escalation),
      "progressive_disclosure": list(self.progressive_disclosure),
      "working_memory_rules": list(self.working_memory_rules),
      "memory_governance": list(self.memory_governance),
      "capability_navigation": [entry.to_context() for entry in self.capability_navigation],
    }


class CoreAgentContextProvider:
  """Default core context provider used by all seven-layer context packs."""

  kind = ContextLayerKind.SYSTEM_POLICY

  def __init__(self, core_context: CoreAgentContext | None = None) -> None:
    self._core_context = core_context or default_core_agent_context()

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    content = self._core_context.to_context()
    item = ContextLayerItem(
      item_id=new_id("ctx_item"),
      layer=self.kind,
      role="system",
      content=content,
      token_estimate=estimate_tokens(content),
      priority=0.98,
      source_type="instruction",
      source_ref="core_agent_context",
      sensitivity="internal",
      rationale="Always include compact core agent operating rules and capability navigation.",
    )
    return ContextLayer(
      kind=self.kind,
      budget_tokens=budget_tokens,
      items=[item],
      token_estimate=item.token_estimate,
    )


def default_core_agent_context() -> CoreAgentContext:
  return CoreAgentContext(
    constitution=(
      "Act through Meadow tools, Skills, Workflows, MCP, workbenches, and sub-agents when they are needed; do not only say you can do something.",
      "Reason from the user goal, current context, Skill/SOP index, tool schemas, memory, and real tool results.",
      "All side effects must go through CapabilityRuntime, policy grants, approvals, audit, and events.",
      "Ask the user only when required information, authorization, or an irreversible decision cannot be inferred safely.",
    ),
    failure_escalation=(
      "On the first failure, read and explain the concrete error before retrying.",
      "On the second related failure, inspect environment state such as logs, targets, files, events, browser tabs, or connector status.",
      "On the third related failure, switch strategy, use another available capability, or request user input. Do not repeat without new evidence.",
    ),
    progressive_disclosure=(
      "The default context contains compact SkillCard and capability indexes, not full SOP bodies.",
      "When a task needs procedure details, call skill_open, skill_resource_open, or context_expand before executing the SOP.",
      "Open artifact, event, memory, or Skill resources only when the compact index is insufficient.",
    ),
    working_memory_rules=(
      "Use memory_checkpoint for non-trivial tasks after reading a relevant SOP, before subtask switches, before context compression, and after repeated failures.",
      "Working memory should keep the active goal, user constraints, selected SOPs, active run/task/workbench IDs, failure findings, and next step.",
      "Do not checkpoint obvious one-turn facts or stale state from a previous unrelated task.",
    ),
    memory_governance=(
      "No execution, no memory: long-term facts and procedures must come from successful tool results, events, artifacts, or user-confirmed facts.",
      "Do not store volatile session IDs, temporary PIDs, transient timestamps, secrets, or guesses as long-term memory.",
      "Large content belongs in artifacts; prompts and events should carry summaries and refs.",
    ),
    capability_navigation=(
      CapabilityNavigationEntry(
        topic="web_research_and_browser",
        triggers=("搜索", "今天/当前/最近", "打开网页", "浏览器", "小红书", "天气", "新闻"),
        skill_ids=("builtin.atomic.web_research", "builtin.atomic.desktop_mobile_control"),
        tools=("skill_open", "skill_resource_open", "browser_scan", "browser_navigate", "browser_execute_js", "http_request"),
        resources=("builtin.sop.browser_research",),
      ),
      CapabilityNavigationEntry(
        topic="multi_agent_collaboration",
        triggers=("多个agent", "并行搜索", "群聊", "CLI协作", "技术评审工作台", "持续任务"),
        skill_ids=("builtin.atomic.collaboration_workbench", "builtin.atomic.agent_delegation"),
        tools=("skill_open", "skill_resource_open", "workbench_create", "workbench_status", "workbench_message", "agent_delegate"),
        resources=("builtin.sop.delegation",),
      ),
      CapabilityNavigationEntry(
        topic="planning_and_verification",
        triggers=("复杂任务", "多步骤", "计划", "验收", "验证", "端到端"),
        skill_ids=("builtin.atomic.code_execution",),
        tools=("skill_open", "skill_resource_open", "memory_checkpoint", "code_execute", "user_input_request"),
        resources=("builtin.sop.planning", "builtin.sop.verification"),
      ),
      CapabilityNavigationEntry(
        topic="code_and_workspace",
        triggers=("代码", "文件", "项目", "测试", "修改", "评审"),
        skill_ids=("builtin.atomic.workspace", "builtin.atomic.code_execution"),
        tools=("workspace_read", "workspace_patch", "workspace_write", "code_execute"),
        resources=("builtin.sop.review",),
      ),
      CapabilityNavigationEntry(
        topic="memory_and_skill_evolution",
        triggers=("记忆", "经验", "沉淀", "skill", "SOP", "上下文"),
        skill_ids=("builtin.atomic.memory_checkpoint",),
        tools=("skill_open", "skill_resource_open", "memory_checkpoint", "memory_evolution_note", "memory_search", "memory_read"),
        resources=("builtin.sop.memory_governance",),
      ),
      CapabilityNavigationEntry(
        topic="user_input_and_approval",
        triggers=("缺少信息", "授权", "审批", "确认", "不可逆"),
        skill_ids=("builtin.atomic.user_input",),
        tools=("user_input_request",),
        resources=(),
      ),
    ),
  )
