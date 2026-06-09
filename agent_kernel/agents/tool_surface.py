"""Tool-surface policy for continuous agent loops.

The policy keeps the runtime capability set broad while exposing a smaller,
task-relevant tool surface to the model. It is deliberately heuristic today,
but hidden behind an interface so it can be replaced by model routing,
embedding retrieval, or memory-backed skill selection later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Protocol

from agent_kernel.domain.skill import SkillCard


@dataclass(slots=True)
class ToolSurfaceSelection:
  skills: list[SkillCard]
  tool_schemas: list[dict]
  allowed_tool_names: list[str] = field(default_factory=list)
  rationale: str = ""


class ToolSurfacePolicy(Protocol):
  def select(
    self,
    *,
    user_goal: str,
    skills: list[SkillCard],
    tool_schemas: list[dict],
    turn: int,
  ) -> ToolSurfaceSelection:
    ...


class SkillAwareToolSurfacePolicy:
  """Selects active skills and recommended tools for the current user goal."""

  _CORE_CONTEXT_TOOLS = {
    "user_input_request",
    "skill_open",
    "skill_resource_open",
    "memory_search",
    "memory_read",
    "artifact_read",
    "context_expand",
    "memory_checkpoint",
  }
  _DOMAIN_TERMS_BY_SKILL_ID = {
    "builtin.atomic.web_research": [
      "browser",
      "web",
      "网页",
      "浏览器",
      "搜索",
      "查询",
      "打开",
      "刷新",
      "页面",
      "网站",
      "今天",
      "当前",
      "最新",
      "推荐",
      "帖子",
      "天气",
      "新闻",
      "资料",
      "链接",
    ],
    "builtin.atomic.desktop_mobile_control": [
      "桌面",
      "窗口",
      "点击",
      "键盘",
      "输入",
      "截图",
      "屏幕",
      "手机",
      "移动端",
      "app",
      "ui",
    ],
    "builtin.atomic.workspace": ["文件", "代码", "修改", "写入", "读取", "补丁", "项目", "目录", "保存"],
    "builtin.atomic.code_execution": ["执行", "运行", "命令", "终端", "脚本", "验证", "测试", "计算"],
    "builtin.atomic.agent_delegation": ["子 agent", "子代理", "并发", "分配", "委派", "多个 agent", "搜索调研"],
    "builtin.atomic.collaboration_workbench": ["群聊", "工作台", "协同", "评审", "review", "cli", "主持人"],
    "builtin.atomic.memory_checkpoint": ["记忆", "上下文", "总结", "沉淀", "经验", "checkpoint"],
  }

  def __init__(self, *, max_selected_skills: int = 3) -> None:
    self._max_selected_skills = max(1, max_selected_skills)

  def select(
    self,
    *,
    user_goal: str,
    skills: list[SkillCard],
    tool_schemas: list[dict],
    turn: int,
  ) -> ToolSurfaceSelection:
    if not skills:
      return ToolSurfaceSelection(
        skills=[],
        tool_schemas=tool_schemas,
        allowed_tool_names=[name for name, _ in _named_tool_schemas(tool_schemas)],
        rationale="no_skills_configured",
      )
    ranked = self._rank_skills(user_goal, skills)
    selected = [skill for score, skill in ranked if score > 0][: self._max_selected_skills]
    if not selected:
      fallback_skills = skills if len(skills) <= self._max_selected_skills else []
      return ToolSurfaceSelection(
        skills=fallback_skills,
        tool_schemas=tool_schemas,
        allowed_tool_names=[name for name, _ in _named_tool_schemas(tool_schemas)],
        rationale="no_skill_match_full_surface",
      )
    allowed_names = self._allowed_tool_names(selected, turn=turn)
    selected_schemas = [
      schema
      for name, schema in _named_tool_schemas(tool_schemas)
      if name in allowed_names
    ]
    if not selected_schemas:
      selected_schemas = tool_schemas
      allowed_names = {name for name, _ in _named_tool_schemas(tool_schemas)}
    return ToolSurfaceSelection(
      skills=selected,
      tool_schemas=selected_schemas,
      allowed_tool_names=sorted(allowed_names),
      rationale="skill_recommended_tools",
    )

  def _rank_skills(self, user_goal: str, skills: list[SkillCard]) -> list[tuple[int, SkillCard]]:
    goal = user_goal.lower()
    terms = set(_terms(user_goal))
    ranked: list[tuple[int, SkillCard]] = []
    for skill in skills:
      text = " ".join(
        [
          skill.skill_id,
          skill.name,
          skill.description,
          skill.when_to_use,
          " ".join(skill.recommended_tools),
        ]
      ).lower()
      score = 0
      score += sum(3 for term in self._DOMAIN_TERMS_BY_SKILL_ID.get(skill.skill_id, []) if term.lower() in goal)
      score += sum(1 for term in terms if term and term in text)
      if any(tool in goal for tool in skill.recommended_tools):
        score += 2
      ranked.append((score, skill))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked

  def _allowed_tool_names(self, selected: list[SkillCard], *, turn: int) -> set[str]:
    allowed: set[str] = set(self._CORE_CONTEXT_TOOLS)
    for skill in selected:
      allowed.update(skill.recommended_tools)
    if turn > 1:
      allowed.update({"context_compact", "event_search"})
    return allowed


def _named_tool_schemas(tool_schemas: list[dict]) -> list[tuple[str, dict]]:
  named: list[tuple[str, dict]] = []
  for schema in tool_schemas:
    function = schema.get("function") if isinstance(schema, dict) else None
    name = function.get("name") if isinstance(function, dict) else None
    if isinstance(name, str) and name:
      named.append((name, schema))
  return named


def _terms(text: str) -> list[str]:
  return [term.lower() for term in re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]{2,}", text)]
