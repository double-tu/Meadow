"""Seven-layer context assembly."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from agent_kernel.context.budget import ContextBudgetManager
from agent_kernel.context.core import CoreAgentContextProvider
from agent_kernel.context.event_payload import compact_context_built_payload
from agent_kernel.context.token_counter import estimate_tokens
from agent_kernel.domain.base import new_id
from agent_kernel.domain.context import (
  ContextAssemblyRequest,
  ContextCandidate,
  ContextLayer,
  ContextLayerItem,
  ContextLayerKind,
  ContextPack,
  ContextPlan,
  ModelContext,
  RetrievalPack,
)
from agent_kernel.domain.events import RuntimeEvent, RuntimeEventType
from agent_kernel.domain.identifiers import ArtifactRef, MemoryRef
from agent_kernel.domain.memory import MemoryItem
from agent_kernel.domain.skill import SkillCard
from agent_kernel.memory.facade import MemoryFacade


class ContextLayerProvider(Protocol):
  kind: ContextLayerKind

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    ...


class ContextAssembler:
  """Builds model context from seven independently pluggable layers."""

  def __init__(
    self,
    *,
    uow_factory,
    memory: MemoryFacade | None = None,
    budget_manager: ContextBudgetManager | None = None,
    providers: list[ContextLayerProvider] | None = None,
  ) -> None:
    self._uow_factory = uow_factory
    self._memory = memory
    self._budget_manager = budget_manager or ContextBudgetManager()
    self._providers = providers or self._default_providers()

  def assemble(self, request: ContextAssemblyRequest) -> ContextPack:
    budget = self._budget_manager.allocate(request.max_tokens, request.layer_weights)
    layers: list[ContextLayer] = []
    omitted: list[ContextLayerItem] = []
    for provider in self._providers:
      layer_budget = budget.layer_budgets.get(provider.kind.value, 0)
      layer = provider.collect(request, layer_budget)
      trimmed = self._budget_manager.trim_layer(layer, layer_budget)
      if provider.kind == ContextLayerKind.CONVERSATION_WINDOW:
        trimmed = _repair_conversation_tool_transcript(layer, trimmed)
      layers.append(trimmed)
      omitted.extend(trimmed.omitted_items)
    messages = _render_model_messages(layers)
    memory_refs = _unique_memory_refs(
      item.memory_ref
      for layer in layers
      for item in layer.items
      if item.memory_ref is not None
    )
    artifact_refs = _unique_artifact_refs(
      ref
      for layer in layers
      for item in layer.items
      for ref in item.artifact_refs
    )
    model_context = ModelContext(
      messages=messages,
      tool_schemas=request.tool_schemas,
      attachments=artifact_refs,
      memory_refs=memory_refs,
      omitted_candidates=[
        item.memory_ref
        for item in omitted
        if item.memory_ref is not None
      ],
      token_budget_ledger=budget.layer_budgets,
      inclusion_rationale={
        item.source_ref or item.item_id: item.rationale or f"Selected from {item.layer.value}."
        for layer in layers
        for item in layer.items
      },
    )
    pack = ContextPack(
      pack_id=new_id("ctx_pack"),
      run_id=request.run_id,
      model_ref=request.model_ref,
      layers=layers,
      model_context=model_context,
      omitted_items=omitted,
      quality_warnings=_quality_warnings(layers),
      token_budget_ledger=budget.layer_budgets,
    )
    self._write_ledger(request, pack)
    return pack

  def _default_providers(self) -> list[ContextLayerProvider]:
    return [
      SystemPolicyLayerProvider(),
      CoreAgentContextProvider(),
      AgentProfileLayerProvider(),
      SkillToolIndexLayerProvider(),
      WorkingMemoryLayerProvider(self._memory),
      ConversationWindowLayerProvider(),
      EpisodicArtifactLayerProvider(self._memory),
      LongTermMemoryLayerProvider(self._memory),
    ]

  def _write_ledger(self, request: ContextAssemblyRequest, pack: ContextPack) -> None:
    candidates = [
      ContextCandidate(
        candidate_id=item.item_id,
        source_type=_candidate_source_type(item.source_type),
        source_ref=item.source_ref or item.item_id,
        token_estimate=item.token_estimate,
        relevance_score=item.priority,
        sensitivity=item.sensitivity,
        include=True,
        rationale=item.rationale,
      )
      for layer in pack.layers
      for item in layer.items
    ]
    omitted_candidates = [
      ContextCandidate(
        candidate_id=item.item_id,
        source_type=_candidate_source_type(item.source_type),
        source_ref=item.source_ref or item.item_id,
        token_estimate=item.token_estimate,
        relevance_score=item.priority,
        sensitivity=item.sensitivity,
        include=False,
        rationale=item.rationale,
      )
      for item in pack.omitted_items
    ]
    plan = ContextPlan(
      plan_id=new_id("ctx_plan"),
      run_id=request.run_id,
      model_ref=request.model_ref,
      partition_budgets=pack.token_budget_ledger,
      candidates=[*candidates[:50], *omitted_candidates[:50]],
      selected_candidate_ids=[candidate.candidate_id for candidate in candidates],
      omitted_candidate_ids=[candidate.candidate_id for candidate in omitted_candidates],
      tool_visibility=[str(tool.get("name", tool.get("capability_id", ""))) for tool in request.tool_schemas],
      compression_strategy="seven_layer_budget_trim",
      density_score=_density_score(pack.layers),
      quality_warnings=pack.quality_warnings,
    )
    retrieval_pack = RetrievalPack(
      working_snapshot={
        "layers": [
          {
            "kind": layer.kind.value,
            "selected": len(layer.items),
            "omitted": len(layer.omitted_items),
            "tokens": layer.token_estimate,
          }
          for layer in pack.layers
        ]
      },
      episodic_refs=[
        ref for ref in pack.model_context.memory_refs if ref.memory_type == "episodic"
      ],
      semantic_refs=[
        ref for ref in pack.model_context.memory_refs if ref.memory_type == "semantic"
      ],
      procedural_refs=[
        ref for ref in pack.model_context.memory_refs if ref.memory_type == "procedural"
      ],
      artifact_refs=pack.model_context.attachments,
      token_estimate=sum(layer.token_estimate for layer in pack.layers),
    )
    payload = compact_context_built_payload(
      {
        "context_plan": plan.to_dict(),
        "retrieval_pack": retrieval_pack.to_dict(),
        "context_pack_id": pack.pack_id,
      }
    )
    with self._uow_factory() as uow:
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.CONTEXT_BUILT,
          run_id=request.run_id,
          agent_id=request.agent_id,
          task_id=request.task_id,
          payload=payload,
          artifact_refs=pack.model_context.attachments,
        )
      )


class SystemPolicyLayerProvider:
  kind = ContextLayerKind.SYSTEM_POLICY

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    content = request.system_instructions or (
      "你是 Meadow Agent。请遵守权限、审批和安全策略；大内容只通过 artifact ref 引用；"
      "需要更多细节时主动调用可用的读取、搜索或 skill 打开工具。"
    )
    return _layer(
      self.kind,
      budget_tokens,
      [
        _item(
          self.kind,
          role="system",
          content=content,
          priority=1.0,
          source_type="instruction",
          source_ref="system_policy",
          rationale="Always include compact system and policy instructions.",
        )
      ],
    )


class AgentProfileLayerProvider:
  kind = ContextLayerKind.AGENT_PROFILE

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    profile = {
      "type": "agent_profile",
      "agent_id": request.agent_id,
      "task_id": request.task_id,
      "scope": request.scope,
      "model_ref": request.model_ref,
      **request.agent_profile,
    }
    return _layer(
      self.kind,
      budget_tokens,
      [
        _item(
          self.kind,
          role="system",
          content=profile,
          priority=0.9,
          source_type="instruction",
          source_ref=request.agent_id or request.scope,
          rationale="Identify the active agent, scope, model, and host profile.",
        )
      ],
    )


class SkillToolIndexLayerProvider:
  kind = ContextLayerKind.SKILL_TOOL_INDEX

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    skills = [_skill_index(skill) for skill in request.skills if isinstance(skill, SkillCard)]
    tools = [
      {
        "name": _tool_schema_name(tool),
        "description": _tool_schema_description(tool),
        "capability_id": tool.get("capability_id"),
      }
      for tool in request.tool_schemas
    ]
    if not skills and not tools:
      return _layer(self.kind, budget_tokens, [])
    return _layer(
      self.kind,
      budget_tokens,
      [
        _item(
          self.kind,
          role="system",
          content={
            "type": "skill_tool_index",
            "disclosure": "compact_index_only",
            "skills": skills,
            "tools": tools,
            "instruction": (
              "Use this index to decide what to call. Do not assume full skill details are loaded; "
              "open/read the skill or resource when detailed procedure is needed."
            ),
          },
          priority=0.86,
          source_type="schema",
          source_ref="skill_tool_index",
          rationale="Expose available capabilities without loading full skill instructions.",
        )
      ],
    )


class WorkingMemoryLayerProvider:
  kind = ContextLayerKind.WORKING_MEMORY

  def __init__(self, memory: MemoryFacade | None) -> None:
    self._memory = memory

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    items = []
    state = request.metadata.get("working_memory")
    if isinstance(state, dict) and state:
      items.append(
        _item(
          self.kind,
          role="system",
          content={"type": "working_state", **state},
          priority=0.95,
          source_type="workflow_state",
          source_ref=f"{request.scope}:working_state",
          rationale="Host supplied active working state.",
        )
      )
    items.extend(
      _memory_layer_items(
        self.kind,
        self._retrieve(request, "working", limit=20),
        role="system",
        base_priority=0.82,
        allowed_sensitivities=request.allowed_sensitivities,
      )
    )
    return _layer(self.kind, budget_tokens, items)

  def _retrieve(self, request: ContextAssemblyRequest, memory_type: str, limit: int) -> list[MemoryItem]:
    if self._memory is None:
      return []
    return self._memory.retrieve(request.scope, memory_type=memory_type, limit=limit)


class ConversationWindowLayerProvider:
  kind = ContextLayerKind.CONVERSATION_WINDOW

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    count = len(request.messages)
    items = []
    for index, message in enumerate(request.messages):
      role = str(message.get("role") or "user")
      content = message.get("content")
      if content is None and not (role == "assistant" and isinstance(message.get("tool_calls"), list)):
        continue
      freshness = (index + 1) / max(1, count)
      items.append(
        _item(
          self.kind,
          role=role if role in {"system", "user", "assistant", "tool"} else "user",
          content=_conversation_token_content(message, content),
          priority=0.55 + freshness,
          source_type="message",
          source_ref=str(message.get("message_id") or f"message:{index}"),
          rationale="Recent conversation window message.",
          message=message,
        )
      )
    return _layer(self.kind, budget_tokens, items)


class EpisodicArtifactLayerProvider:
  kind = ContextLayerKind.EPISODIC_ARTIFACT

  def __init__(self, memory: MemoryFacade | None) -> None:
    self._memory = memory

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    memories = []
    if self._memory is not None:
      memories.extend(self._memory.retrieve(request.scope, memory_type="episodic", limit=10))
      memories.extend(self._memory.retrieve(request.scope, memory_type="artifact", limit=10))
    return _layer(
      self.kind,
      budget_tokens,
      _memory_layer_items(
        self.kind,
        memories,
        role="system",
        base_priority=0.64,
        allowed_sensitivities=request.allowed_sensitivities,
      ),
    )


class LongTermMemoryLayerProvider:
  kind = ContextLayerKind.LONG_TERM_MEMORY

  def __init__(self, memory: MemoryFacade | None) -> None:
    self._memory = memory

  def collect(self, request: ContextAssemblyRequest, budget_tokens: int) -> ContextLayer:
    memories = []
    if self._memory is not None:
      memories.extend(self._memory.retrieve(request.scope, memory_type="semantic", limit=10))
      memories.extend(self._memory.retrieve(request.scope, memory_type="procedural", limit=10))
    return _layer(
      self.kind,
      budget_tokens,
      _memory_layer_items(
        self.kind,
        memories,
        role="system",
        base_priority=0.58,
        allowed_sensitivities=request.allowed_sensitivities,
      ),
    )


def _layer(kind: ContextLayerKind, budget_tokens: int, items: list[ContextLayerItem]) -> ContextLayer:
  return ContextLayer(
    kind=kind,
    budget_tokens=budget_tokens,
    items=items,
    token_estimate=sum(item.token_estimate for item in items),
  )


def _item(
  layer: ContextLayerKind,
  *,
  role: str,
  content: Any,
  priority: float,
  source_type: str,
  source_ref: str | None,
  rationale: str,
  memory_ref: MemoryRef | None = None,
  artifact_refs: list[ArtifactRef] | None = None,
  sensitivity: str = "internal",
  message: dict[str, Any] | None = None,
) -> ContextLayerItem:
  return ContextLayerItem(
    item_id=new_id("ctx_item"),
    layer=layer,
    role=role,
    content=content,
    token_estimate=estimate_tokens(content),
    priority=priority,
    source_type=source_type,
    source_ref=source_ref,
    memory_ref=memory_ref,
    artifact_refs=artifact_refs or [],
    sensitivity=sensitivity,  # type: ignore[arg-type]
    rationale=rationale,
    message=message,
  )


def _memory_layer_items(
  layer: ContextLayerKind,
  memories: list[MemoryItem],
  *,
  role: str,
  base_priority: float,
  allowed_sensitivities: set[str],
) -> list[ContextLayerItem]:
  items: list[ContextLayerItem] = []
  for memory in memories:
    if memory.sensitivity not in allowed_sensitivities:
      continue
    priority = base_priority + (memory.importance or 0.0) * 0.3
    items.append(
      _item(
        layer,
        role=role,
        content={
          "type": f"{memory.memory_type}_memory",
          "memory_id": memory.memory_id,
          "content": memory.content,
          "artifact_refs": [ref.to_dict() for ref in memory.source_artifact_refs],
        },
        priority=priority,
        source_type="memory",
        source_ref=memory.memory_id,
        memory_ref=MemoryRef(memory_id=memory.memory_id, memory_type=memory.memory_type, score=priority),
        artifact_refs=memory.source_artifact_refs,
        sensitivity=memory.sensitivity,
        rationale=f"Selected {memory.memory_type} memory by layer priority and importance.",
      )
    )
  return items


def _skill_index(skill: SkillCard) -> dict[str, Any]:
  required_grants = []
  require_human_approval = False
  max_risk_level = None
  if skill.use_policy is not None:
    required_grants = skill.use_policy.required_grants
    require_human_approval = skill.use_policy.require_human_approval
    max_risk_level = skill.use_policy.max_risk_level
  return {
    "skill_id": skill.skill_id,
    "name": skill.name,
    "description": skill.description,
    "when_to_use": skill.when_to_use,
    "procedure_hint": _skill_procedure_hint(skill),
    "execution_mode": str(skill.execution_mode),
    "recommended_tools": skill.recommended_tools,
    "recommended_workflows": skill.recommended_workflows,
    "required_grants": required_grants,
    "require_human_approval": require_human_approval,
    "max_risk_level": max_risk_level,
    "procedure_memory_ref": skill.procedure_memory_ref.to_dict() if skill.procedure_memory_ref else None,
    "compiled_workflow_ref": skill.compiled_workflow_ref,
    "resource_refs": list(skill.resource_refs),
  }


def _skill_procedure_hint(skill: SkillCard, *, max_chars: int = 420) -> str | None:
  instructions = skill.instructions
  if not isinstance(instructions, str):
    return None
  compact = " ".join(instructions.split())
  if not compact:
    return None
  explicit_hint = _extract_index_hint(instructions)
  if explicit_hint is not None:
    return explicit_hint if len(explicit_hint) <= max_chars else explicit_hint[: max_chars - 16].rstrip() + "...[open skill]"
  if not skill.skill_id.startswith("builtin.") and not compact.startswith("SOP:"):
    return None
  if len(compact) <= max_chars:
    return compact
  return compact[: max_chars - 16].rstrip() + "...[open skill]"


def _extract_index_hint(instructions: str) -> str | None:
  marker = "INDEX_HINT:"
  for line in instructions.splitlines():
    stripped = line.strip()
    if stripped.startswith(marker):
      hint = stripped[len(marker) :].strip()
      return hint or None
  return None


def _unique_memory_refs(refs: Any) -> list[MemoryRef]:
  unique: dict[str, MemoryRef] = {}
  for ref in refs:
    unique.setdefault(ref.memory_id, ref)
  return list(unique.values())


def _unique_artifact_refs(refs: Any) -> list[ArtifactRef]:
  unique: dict[str, ArtifactRef] = {}
  for ref in refs:
    unique.setdefault(ref.artifact_id, ref)
  return list(unique.values())


def _quality_warnings(layers: list[ContextLayer]) -> list[str]:
  warnings = []
  if any(layer.omitted_items for layer in layers):
    warnings.append("context_layer_budget_omitted_items")
  if any(layer.kind is ContextLayerKind.SKILL_TOOL_INDEX and layer.omitted_items for layer in layers):
    warnings.append("skill_tool_index_omitted_items")
  return warnings


def _density_score(layers: list[ContextLayer]) -> float:
  budget = sum(layer.budget_tokens for layer in layers)
  if budget <= 0:
    return 0.0
  useful = sum(int(item.token_estimate * item.priority) for layer in layers for item in layer.items)
  return round(min(1.0, useful / budget), 4)


def _candidate_source_type(source_type: str) -> Any:
  allowed = {"message", "memory", "artifact", "tool_result", "workflow_state", "schema", "instruction"}
  return source_type if source_type in allowed else "instruction"


def _repair_conversation_tool_transcript(original: ContextLayer, trimmed: ContextLayer) -> ContextLayer:
  if original.kind != ContextLayerKind.CONVERSATION_WINDOW:
    return trimmed
  selected_ids = {item.item_id for item in trimmed.items}
  original_items = list(original.items)
  changed = True
  while changed:
    changed = False
    for index, item in enumerate(original_items):
      if item.item_id not in selected_ids:
        continue
      before_count = len(selected_ids)
      message = item.message or {}
      role = str(message.get("role") or item.role)
      if role == "assistant":
        call_ids = _message_tool_call_ids(message)
        if call_ids:
          preceding_user = _preceding_user_item(original_items, start_index=index)
          if preceding_user is not None:
            selected_ids.add(preceding_user.item_id)
          selected_ids.update(
            response.item_id
            for response in _following_tool_response_items(original_items, start_index=index, call_ids=call_ids)
          )
      elif role == "tool":
        tool_call_id = message.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
          assistant = _preceding_tool_call_item(original_items, start_index=index, tool_call_id=tool_call_id)
          if assistant is not None:
            selected_ids.add(assistant.item_id)
            assistant_index = original_items.index(assistant)
            preceding_user = _preceding_user_item(original_items, start_index=assistant_index)
            if preceding_user is not None:
              selected_ids.add(preceding_user.item_id)
      elif role == "user" and _is_runtime_continuation_message(message):
        selected_ids.update(
          group_item.item_id
          for group_item in _preceding_tool_transcript_group(original_items, start_index=index)
        )
      if len(selected_ids) != before_count:
        changed = True
  selected = [item for item in original_items if item.item_id in selected_ids]
  omitted = [item for item in original_items if item.item_id not in selected_ids]
  return ContextLayer(
    kind=trimmed.kind,
    budget_tokens=trimmed.budget_tokens,
    items=selected,
    omitted_items=[*original.omitted_items, *omitted],
    token_estimate=sum(item.token_estimate for item in selected),
    rationale=trimmed.rationale,
  )


def _message_tool_call_ids(message: dict[str, Any]) -> set[str]:
  tool_calls = message.get("tool_calls")
  if not isinstance(tool_calls, list):
    return set()
  call_ids: set[str] = set()
  for call in tool_calls:
    if not isinstance(call, dict):
      continue
    call_id = call.get("id") or call.get("tool_call_id")
    if isinstance(call_id, str) and call_id:
      call_ids.add(call_id)
  return call_ids


def _following_tool_response_items(
  items: list[ContextLayerItem],
  *,
  start_index: int,
  call_ids: set[str],
) -> list[ContextLayerItem]:
  responses: list[ContextLayerItem] = []
  remaining = set(call_ids)
  for item in items[start_index + 1 :]:
    message = item.message or {}
    role = str(message.get("role") or item.role)
    if role == "assistant":
      break
    if role != "tool":
      continue
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id in remaining:
      responses.append(item)
      remaining.remove(tool_call_id)
    if not remaining:
      break
  return responses


def _preceding_tool_call_item(
  items: list[ContextLayerItem],
  *,
  start_index: int,
  tool_call_id: str,
) -> ContextLayerItem | None:
  for item in reversed(items[:start_index]):
    message = item.message or {}
    role = str(message.get("role") or item.role)
    if role == "assistant":
      return item if tool_call_id in _message_tool_call_ids(message) else None
    if role == "user":
      return None
  return None


def _preceding_user_item(items: list[ContextLayerItem], *, start_index: int) -> ContextLayerItem | None:
  for item in reversed(items[:start_index]):
    message = item.message or {}
    role = str(message.get("role") or item.role)
    if role == "user":
      return item
    if role == "assistant":
      return None
  return None


def _is_runtime_continuation_message(message: dict[str, Any]) -> bool:
  content = message.get("content")
  if not isinstance(content, dict):
    return False
  return content.get("type") in {"runtime_context", "model_repair"}


def _preceding_tool_transcript_group(
  items: list[ContextLayerItem],
  *,
  start_index: int,
) -> list[ContextLayerItem]:
  group: list[ContextLayerItem] = []
  saw_tool = False
  saw_assistant = False
  for item in reversed(items[:start_index]):
    message = item.message or {}
    role = str(message.get("role") or item.role)
    if role == "tool":
      saw_tool = True
      group.append(item)
      continue
    if role == "assistant" and saw_tool and _message_tool_call_ids(message):
      saw_assistant = True
      group.append(item)
      continue
    if role == "user" and saw_assistant:
      group.append(item)
      break
    if role == "user":
      break
  return list(reversed(group))


def _conversation_token_content(message: dict[str, Any], content: Any) -> Any:
  role = str(message.get("role") or "user")
  if role in {"assistant", "tool"}:
    return message
  return content


def _render_model_messages(layers: list[ContextLayer]) -> list[dict[str, Any]]:
  system_parts: list[str] = []
  context_sections: list[str] = []
  transcript: list[dict[str, Any]] = []
  for layer in layers:
    if layer.kind == ContextLayerKind.SYSTEM_POLICY:
      for item in layer.items:
        system_parts.append(_render_system_item(item))
      continue
    if layer.kind == ContextLayerKind.CONVERSATION_WINDOW:
      for item in layer.items:
        message = _message_from_layer_item(item)
        role = str(message.get("role") or "user")
        if role == "system":
          context_sections.append(_render_context_item(item))
        else:
          transcript.append(message)
      continue
    if layer.items:
      context_sections.append(_render_context_layer(layer))
  messages: list[dict[str, Any]] = []
  system_content = "\n\n".join(part for part in system_parts if part.strip()).strip()
  if system_content:
    messages.append({"role": "system", "content": system_content})
  context_content = "\n\n".join(section for section in context_sections if section.strip()).strip()
  if context_content:
    messages.append({"role": "user", "name": "context", "content": context_content})
  messages.extend(transcript)
  return messages


def _render_system_item(item: ContextLayerItem) -> str:
  if isinstance(item.content, str):
    return item.content
  if isinstance(item.content, dict) and item.content.get("type") == "core_agent_context":
    return _render_core_agent_context(item.content)
  return _render_tagged_section(str(item.source_ref or item.layer.value), item.content)


def _render_core_agent_context(content: dict[str, Any]) -> str:
  lines = ["# Meadow Operating Context", "", "This is compact operating context, not a fixed workflow."]
  for title, key in [
    ("Constitution", "constitution"),
    ("Progressive Disclosure", "progressive_disclosure"),
    ("Failure Handling", "failure_escalation"),
    ("Working Memory", "working_memory_rules"),
    ("Memory Governance", "memory_governance"),
  ]:
    values = content.get(key)
    if isinstance(values, list) and values:
      lines.extend(["", f"## {title}"])
      lines.extend(f"- {value}" for value in values if isinstance(value, str))
  navigation = content.get("capability_navigation")
  if isinstance(navigation, list) and navigation:
    lines.extend(["", "## Capability Navigation"])
    for entry in navigation:
      if not isinstance(entry, dict):
        continue
      topic = entry.get("topic")
      skills = ", ".join(str(value) for value in entry.get("skill_ids", []) if value)
      tools = ", ".join(str(value) for value in entry.get("tools", []) if value)
      resources = ", ".join(str(value) for value in entry.get("resources", []) if value)
      parts = [str(topic or "topic")]
      if skills:
        parts.append(f"skills: {skills}")
      if tools:
        parts.append(f"tools: {tools}")
      if resources:
        parts.append(f"resources: {resources}")
      lines.append("- " + " | ".join(parts))
  return "\n".join(lines)


def _render_context_layer(layer: ContextLayer) -> str:
  title = _context_layer_title(layer.kind)
  rendered_items = [_render_context_item(item) for item in layer.items]
  rendered_items = [item for item in rendered_items if item.strip()]
  if not rendered_items:
    return ""
  return f"<{title}>\n" + "\n\n".join(rendered_items) + f"\n</{title}>"


def _render_context_item(item: ContextLayerItem) -> str:
  content = item.content
  if item.layer == ContextLayerKind.AGENT_PROFILE:
    return _render_agent_profile(content)
  if item.layer == ContextLayerKind.SKILL_TOOL_INDEX:
    return _render_skill_tool_index(content)
  if item.layer == ContextLayerKind.WORKING_MEMORY:
    return _render_memory_or_state(content)
  if item.layer in {ContextLayerKind.EPISODIC_ARTIFACT, ContextLayerKind.LONG_TERM_MEMORY}:
    return _render_memory_or_state(content)
  if isinstance(content, dict) and content.get("type") == "working_memory_anchor":
    return _render_working_anchor(content)
  return _render_tagged_section(str(item.source_ref or item.layer.value), content)


def _context_layer_title(kind: ContextLayerKind) -> str:
  if kind == ContextLayerKind.AGENT_PROFILE:
    return "agent_profile"
  if kind == ContextLayerKind.SKILL_TOOL_INDEX:
    return "available_skills_and_tools"
  if kind == ContextLayerKind.WORKING_MEMORY:
    return "working_memory"
  if kind == ContextLayerKind.EPISODIC_ARTIFACT:
    return "episodic_context"
  if kind == ContextLayerKind.LONG_TERM_MEMORY:
    return "long_term_memory"
  return kind.value


def _render_agent_profile(content: Any) -> str:
  if not isinstance(content, dict):
    return str(content)
  fields = [
    f"scope: {content.get('scope')}",
    f"agent_id: {content.get('agent_id')}",
    f"task_id: {content.get('task_id')}",
    f"model_ref: {content.get('model_ref')}",
  ]
  return "\n".join(field for field in fields if not field.endswith("None"))


def _render_skill_tool_index(content: Any) -> str:
  if not isinstance(content, dict):
    return str(content)
  lines = [
    "Skill index is compact. Choose relevant skills yourself; open skill/resource details when procedure matters.",
  ]
  skills = content.get("skills")
  if isinstance(skills, list) and skills:
    lines.append("")
    lines.append("Skills:")
    for skill in skills:
      if isinstance(skill, dict):
        lines.extend(_render_skill_card(skill))
  tools = content.get("tools")
  if isinstance(tools, list) and tools:
    lines.append("")
    lines.append("Tools:")
    for tool in tools:
      if not isinstance(tool, dict):
        continue
      name = tool.get("name")
      if not name:
        continue
      description = str(tool.get("description") or "").strip()
      lines.append(f"- {name}: {description[:220]}")
  return "\n".join(lines)


def _render_skill_card(skill: dict[str, Any]) -> list[str]:
  lines = [f"- {skill.get('skill_id')}: {skill.get('name')}"]
  for key, label in [
    ("when_to_use", "when"),
    ("description", "desc"),
    ("procedure_hint", "hint"),
  ]:
    value = skill.get(key)
    if isinstance(value, str) and value.strip():
      lines.append(f"  {label}: {value.strip()}")
  recommended_tools = skill.get("recommended_tools")
  if isinstance(recommended_tools, list) and recommended_tools:
    lines.append("  recommended_tools: " + ", ".join(str(value) for value in recommended_tools if value))
  resource_refs = skill.get("resource_refs")
  if isinstance(resource_refs, list) and resource_refs:
    lines.append("  resource_refs: " + ", ".join(str(value) for value in resource_refs if value))
  return lines


def _render_memory_or_state(content: Any) -> str:
  if not isinstance(content, dict):
    return str(content)
  memory_id = content.get("memory_id")
  content_type = content.get("type")
  if content_type == "working_state":
    return _render_working_state(content)
  payload = content.get("content", content)
  if isinstance(payload, dict):
    summary = payload.get("summary") or payload.get("key_info") or payload.get("active_goal") or _format_json(payload)
  else:
    summary = payload
  prefix = f"- {content_type or 'context'}"
  if memory_id:
    prefix += f" {memory_id}"
  return f"{prefix}: {_compact_render_text(str(summary), 900)}"


def _render_working_state(content: dict[str, Any]) -> str:
  lines = ["- working_state"]
  instructions = content.get("research_instructions")
  if isinstance(instructions, list) and instructions:
    lines.append("  research_instructions:")
    lines.extend(f"  - {item}" for item in instructions if isinstance(item, str))
  ledger = content.get("research_ledger")
  if isinstance(ledger, dict):
    lines.append("  research_ledger:")
    for key in ("strategy_notes", "candidate_sources", "evidence", "failures", "visited_sources"):
      values = ledger.get(key)
      if not isinstance(values, list) or not values:
        continue
      lines.append(f"    {key}:")
      for value in values[-6:]:
        lines.append("    - " + _compact_render_text(_format_json(value) if isinstance(value, dict) else str(value), 500))
  return "\n".join(lines)


def _render_working_anchor(content: dict[str, Any]) -> str:
  lines = ["<working_memory_anchor>"]
  goal = content.get("original_user_goal")
  if goal:
    lines.append(f"original_user_goal: {goal}")
  current_turn = content.get("current_turn")
  if current_turn is not None:
    lines.append(f"current_turn: {current_turn}")
  history = content.get("history")
  if isinstance(history, list) and history:
    lines.append("recent_history:")
    lines.extend(f"- {item}" for item in history[-8:])
  action_history = content.get("action_history")
  if isinstance(action_history, list) and action_history:
    lines.append("action_history:")
    lines.extend(f"- {item}" for item in action_history[-8:])
  lines.append("</working_memory_anchor>")
  return "\n".join(lines)


def _render_tagged_section(name: str, content: Any) -> str:
  normalized = re.sub(r"[^a-zA-Z0-9_:-]+", "_", name).strip("_") or "context"
  if isinstance(content, str):
    body = content
  else:
    body = _format_json(content)
  return f"<{normalized}>\n{body}\n</{normalized}>"


def _format_json(value: Any) -> str:
  return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _compact_render_text(value: str, max_chars: int) -> str:
  text = " ".join(value.split())
  if len(text) <= max_chars:
    return text
  return text[: max_chars - 15].rstrip() + "...[truncated]"


def _tool_schema_name(tool: dict[str, Any]) -> str | None:
  name = tool.get("name") or tool.get("capability_id")
  function = tool.get("function")
  if not name and isinstance(function, dict):
    name = function.get("name")
  return str(name) if name else None


def _tool_schema_description(tool: dict[str, Any]) -> str | None:
  description = tool.get("description")
  function = tool.get("function")
  if not description and isinstance(function, dict):
    description = function.get("description")
  return str(description) if description else None


def _message_from_layer_item(item: ContextLayerItem) -> dict[str, Any]:
  if item.message is None:
    return {"role": item.role, "content": item.content}
  message = dict(item.message)
  role = str(message.get("role") or item.role)
  if role not in {"system", "user", "assistant", "tool"}:
    role = item.role if item.role in {"system", "user", "assistant", "tool"} else "user"
  message["role"] = role
  if "content" not in message:
    message["content"] = item.content
  return message
