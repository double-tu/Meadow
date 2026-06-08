"""Seven-layer context assembly."""

from __future__ import annotations

from typing import Any, Protocol

from agent_kernel.context.budget import ContextBudgetManager
from agent_kernel.context.core import CoreAgentContextProvider
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
      layers.append(trimmed)
      omitted.extend(trimmed.omitted_items)
    messages = [
      {"role": item.role, "content": item.content}
      for layer in layers
      for item in layer.items
    ]
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
    with self._uow_factory() as uow:
      uow.events.append(
        RuntimeEvent(
          event_type=RuntimeEventType.CONTEXT_BUILT,
          run_id=request.run_id,
          agent_id=request.agent_id,
          task_id=request.task_id,
          payload={
            "context_plan": plan.to_dict(),
            "retrieval_pack": retrieval_pack.to_dict(),
            "context_pack_id": pack.pack_id,
          },
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
        "name": tool.get("name") or tool.get("capability_id"),
        "description": tool.get("description"),
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
      if content is None:
        continue
      freshness = (index + 1) / max(1, count)
      items.append(
        _item(
          self.kind,
          role=role if role in {"system", "user", "assistant", "tool"} else "user",
          content=content,
          priority=0.55 + freshness,
          source_type="message",
          source_ref=str(message.get("message_id") or f"message:{index}"),
          rationale="Recent conversation window message.",
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
