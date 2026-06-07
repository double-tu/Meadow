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
  SkillEvolutionRecord,
  WorkflowTemplate,
)
from agent_kernel.domain.serialization import to_primitive


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
    return [
      ExplorationAttempt.from_dict(data)
      for data in self._list("attempt", exploration_id)
    ]

  def save_golden_trace(self, trace: GoldenTrace) -> None:
    self._save(trace.trace_id, "golden_trace", trace.source_attempt_id, trace)

  def save_workflow_template(self, template: WorkflowTemplate) -> None:
    self._save(template.template_id, "workflow_template", template.source_trace_id, template)

  def list_workflow_templates(self) -> list[WorkflowTemplate]:
    return [WorkflowTemplate.from_dict(data) for data in self._list("workflow_template", None)]

  def save_skill_evolution(self, record: SkillEvolutionRecord) -> None:
    self._save(record.record_id, "skill_evolution", record.source_trace_id, record)

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
