"""Small event payload helpers for context observability."""

from __future__ import annotations

from typing import Any

from agent_kernel.domain.events import MAX_EVENT_PAYLOAD_BYTES
from agent_kernel.domain.serialization import to_json


def compact_context_built_payload(payload: dict[str, Any]) -> dict[str, Any]:
  """Keep context.built events bounded without changing model context."""

  size = _payload_size(payload)
  if size <= MAX_EVENT_PAYLOAD_BYTES:
    return payload
  context_plan = payload.get("context_plan") if isinstance(payload.get("context_plan"), dict) else {}
  retrieval_pack = payload.get("retrieval_pack") if isinstance(payload.get("retrieval_pack"), dict) else {}
  compacted: dict[str, Any] = {
    "context_pack_id": payload.get("context_pack_id"),
    "context_ledger_truncated": True,
    "full_payload_bytes": size,
    "context_plan": _compact_context_plan(context_plan),
    "retrieval_pack": _compact_retrieval_pack(retrieval_pack),
  }
  if _payload_size(compacted) <= MAX_EVENT_PAYLOAD_BYTES:
    return compacted
  return {
    "context_pack_id": payload.get("context_pack_id"),
    "context_ledger_truncated": True,
    "full_payload_bytes": size,
    "context_plan": {
      "run_id": context_plan.get("run_id"),
      "model_ref": context_plan.get("model_ref"),
      "quality_warnings": _preview_list(context_plan.get("quality_warnings"), limit=10),
    },
  }


def _compact_context_plan(plan: dict[str, Any]) -> dict[str, Any]:
  selected = _as_list(plan.get("selected_candidate_ids"))
  omitted = _as_list(plan.get("omitted_candidate_ids"))
  tools = _as_list(plan.get("tool_visibility"))
  return {
    "plan_id": plan.get("plan_id"),
    "run_id": plan.get("run_id"),
    "model_ref": plan.get("model_ref"),
    "partition_budgets": plan.get("partition_budgets"),
    "candidate_count": len(_as_list(plan.get("candidates"))),
    "selected_candidate_count": len(selected),
    "omitted_candidate_count": len(omitted),
    "selected_candidate_ids_preview": selected[:20],
    "omitted_candidate_ids_preview": omitted[:20],
    "tool_visibility": tools[:40],
    "tool_visibility_count": len(tools),
    "compression_strategy": plan.get("compression_strategy"),
    "density_score": plan.get("density_score"),
    "quality_warnings": _preview_list(plan.get("quality_warnings"), limit=20),
  }


def _compact_retrieval_pack(pack: dict[str, Any]) -> dict[str, Any]:
  artifacts = _as_list(pack.get("artifact_refs"))
  return {
    "working_snapshot": pack.get("working_snapshot"),
    "episodic_ref_count": len(_as_list(pack.get("episodic_refs"))),
    "semantic_ref_count": len(_as_list(pack.get("semantic_refs"))),
    "procedural_ref_count": len(_as_list(pack.get("procedural_refs"))),
    "artifact_refs": artifacts[:20],
    "artifact_ref_count": len(artifacts),
    "confidence_score": pack.get("confidence_score"),
    "sensitivity_marks": _preview_list(pack.get("sensitivity_marks"), limit=20),
    "token_estimate": pack.get("token_estimate"),
  }


def _preview_list(value: Any, *, limit: int) -> list[Any]:
  return _as_list(value)[:limit]


def _as_list(value: Any) -> list[Any]:
  return value if isinstance(value, list) else []


def _payload_size(payload: dict[str, Any]) -> int:
  return len(to_json(payload).encode("utf-8"))
