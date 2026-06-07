"""Autonomy object persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_kernel.domain.autonomy import (
  CandidateStrategy,
  ExplorationAttempt,
  ExplorationTask,
  GoldenTrace,
  ReflectionRecord,
  SkillEvolutionRecord,
  WorkflowTemplate,
)
from agent_kernel.domain.serialization import to_primitive
from agent_kernel.domain.workflow import WorkflowSpec


class AutonomyStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save_exploration(self, task: ExplorationTask) -> None:
    self._save(task.exploration_id, "exploration", None, task)

  def get_exploration(self, exploration_id: str) -> ExplorationTask | None:
    data = self._get(exploration_id)
    return ExplorationTask.from_dict(data) if data is not None else None

  def save_strategy(self, strategy: CandidateStrategy) -> None:
    self._save(strategy.strategy_id, "strategy", strategy.exploration_id, strategy)

  def list_strategies(self, exploration_id: str) -> list[CandidateStrategy]:
    return [
      CandidateStrategy.from_dict(data)
      for data in self._list("strategy", exploration_id)
    ]

  def save_attempt(self, attempt: ExplorationAttempt) -> None:
    self._save(attempt.attempt_id, "attempt", attempt.exploration_id, attempt)

  def list_attempts(self, exploration_id: str) -> list[ExplorationAttempt]:
    attempts = [
      ExplorationAttempt.from_dict(data)
      for data in self._list("attempt", exploration_id)
    ]
    return sorted(attempts, key=lambda item: (item.started_at is None, item.started_at, item.attempt_id))

  def save_golden_trace(self, trace: GoldenTrace) -> None:
    self._save(trace.trace_id, "golden_trace", trace.source_attempt_id, trace)

  def save_workflow_template(self, template: WorkflowTemplate) -> None:
    self._save(template.template_id, "workflow_template", template.source_trace_id, template)

  def list_workflow_templates(self) -> list[WorkflowTemplate]:
    return [WorkflowTemplate.from_dict(data) for data in self._list("workflow_template", None)]

  def save_workflow_spec(self, workflow: WorkflowSpec) -> None:
    self._save(
      f"{workflow.workflow_id}:{workflow.version}",
      "workflow_spec",
      workflow.workflow_id,
      workflow,
    )

  def get_workflow_spec(self, workflow_id: str, version: str) -> WorkflowSpec | None:
    data = self._get(f"{workflow_id}:{version}")
    return WorkflowSpec.from_dict(data) if data is not None else None

  def list_workflow_specs(self, workflow_id: str | None = None) -> list[WorkflowSpec]:
    return [WorkflowSpec.from_dict(data) for data in self._list("workflow_spec", workflow_id)]

  def save_skill_evolution(self, record: SkillEvolutionRecord) -> None:
    self._save(record.record_id, "skill_evolution", record.source_trace_id, record)

  def save_reflection(self, record: ReflectionRecord) -> None:
    self._save(record.reflection_id, "reflection", record.exploration_id, record)

  def list_reflections(self, exploration_id: str) -> list[ReflectionRecord]:
    return [
      ReflectionRecord.from_dict(data)
      for data in self._list("reflection", exploration_id)
    ]

  def save_record(self, record_type: str, record_id: str, value: Any, parent_id: str | None = None) -> None:
    self._save(record_id, record_type, parent_id, value)

  def get_record(self, record_type: str, record_id: str) -> dict[str, Any] | None:
    row = self._conn.execute(
      """
      SELECT record_json
      FROM autonomy_records
      WHERE record_type = ?
        AND record_id = ?
      """,
      (record_type, record_id),
    ).fetchone()
    if row is None:
      return None
    return json.loads(row["record_json"])

  def list_records(self, record_type: str, parent_id: str | None = None) -> list[dict[str, Any]]:
    return self._list(record_type, parent_id)

  def _save(self, record_id: str, record_type: str, parent_id: str | None, value: Any) -> None:
    self._conn.execute(
      """
      INSERT INTO autonomy_records (record_id, record_type, parent_id, record_json, created_at)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(record_id) DO UPDATE SET
        record_json = excluded.record_json
      """,
      (
        record_id,
        record_type,
        parent_id,
        json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True),
        value.created_at.isoformat() if hasattr(value, "created_at") else "",
      ),
    )

  def _get(self, record_id: str) -> dict[str, Any] | None:
    row = self._conn.execute(
      "SELECT record_json FROM autonomy_records WHERE record_id = ?",
      (record_id,),
    ).fetchone()
    if row is None:
      return None
    return json.loads(row["record_json"])

  def _list(self, record_type: str, parent_id: str | None) -> list[dict[str, Any]]:
    if parent_id is None:
      rows = self._conn.execute(
        """
        SELECT record_json
        FROM autonomy_records
        WHERE record_type = ?
        ORDER BY created_at ASC, record_id ASC
        """,
        (record_type,),
      ).fetchall()
    else:
      rows = self._conn.execute(
        """
        SELECT record_json
        FROM autonomy_records
        WHERE record_type = ?
          AND parent_id = ?
        ORDER BY created_at ASC, record_id ASC
        """,
        (record_type, parent_id),
      ).fetchall()
    return [json.loads(row["record_json"]) for row in rows]
