"""SQLite connection and schema management."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runtime_events (
  event_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  node_id TEXT,
  step_id TEXT,
  agent_id TEXT,
  task_id TEXT,
  causal_id TEXT,
  payload_json TEXT NOT NULL,
  artifact_refs_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_events_run_id_timestamp
ON runtime_events(run_id, timestamp);

CREATE TABLE IF NOT EXISTS run_states (
  run_id TEXT PRIMARY KEY,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  state_json TEXT NOT NULL,
  event_id TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_run_id_created_at
ON checkpoints(run_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  uri TEXT NOT NULL,
  media_type TEXT,
  version TEXT,
  checksum TEXT,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_steps (
  step_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  idempotency_key TEXT,
  lease_id TEXT,
  error TEXT,
  state_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_node_steps_run_id_created_at
ON node_steps(run_id, created_at);

CREATE TABLE IF NOT EXISTS dead_letters (
  item_id TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  failure_type TEXT NOT NULL,
  retryable INTEGER NOT NULL,
  item_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_items (
  item_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_items_status_created_at
ON outbox_items(status, created_at);

CREATE TABLE IF NOT EXISTS agent_sessions (
  session_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL,
  parent_session_id TEXT,
  task_id TEXT,
  mailbox_id TEXT,
  state_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_parent_session_id
ON agent_sessions(parent_session_id);

CREATE TABLE IF NOT EXISTS mailbox_messages (
  message_id TEXT PRIMARY KEY,
  mailbox_id TEXT NOT NULL,
  sender_session_id TEXT,
  recipient_session_id TEXT NOT NULL,
  status TEXT NOT NULL,
  message_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mailbox_messages_recipient_status
ON mailbox_messages(recipient_session_id, status, created_at);

CREATE TABLE IF NOT EXISTS agent_task_results (
  result_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  task_id TEXT,
  ok INTEGER NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_task_results_session_id_created_at
ON agent_task_results(session_id, created_at);

CREATE TABLE IF NOT EXISTS approval_requests (
  approval_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_run_status
ON approval_requests(run_id, status, created_at);

CREATE TABLE IF NOT EXISTS capability_grants (
  grant_id TEXT PRIMARY KEY,
  capability_id TEXT NOT NULL,
  agent_id TEXT,
  task_id TEXT,
  run_id TEXT,
  approval_required INTEGER NOT NULL,
  grant_json TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_capability_grants_capability_run
ON capability_grants(capability_id, run_id, expires_at);

CREATE TABLE IF NOT EXISTS audit_records (
  audit_id TEXT PRIMARY KEY,
  run_id TEXT,
  actor_id TEXT,
  action TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  decision TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_records_run_created_at
ON audit_records(run_id, created_at);

CREATE TABLE IF NOT EXISTS tool_calls (
  tool_call_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  capability_id TEXT NOT NULL,
  status TEXT NOT NULL,
  idempotency_key TEXT,
  process_id INTEGER,
  record_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_created_at
ON tool_calls(run_id, created_at);

CREATE TABLE IF NOT EXISTS recovery_jobs (
  recovery_id TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  status TEXT NOT NULL,
  job_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recovery_jobs_target
ON recovery_jobs(target_type, target_id, created_at);

CREATE TABLE IF NOT EXISTS memory_items (
  memory_id TEXT PRIMARY KEY,
  memory_type TEXT NOT NULL,
  scope TEXT NOT NULL,
  sensitivity TEXT NOT NULL,
  importance REAL,
  memory_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_items_scope_type
ON memory_items(scope, memory_type, created_at);

CREATE TABLE IF NOT EXISTS autonomy_records (
  record_id TEXT PRIMARY KEY,
  record_type TEXT NOT NULL,
  parent_id TEXT,
  record_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_autonomy_records_type_parent
ON autonomy_records(record_type, parent_id, created_at);

CREATE TABLE IF NOT EXISTS interaction_records (
  record_id TEXT PRIMARY KEY,
  record_type TEXT NOT NULL,
  parent_id TEXT,
  record_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interaction_records_type_parent
ON interaction_records(record_type, parent_id, created_at);
"""


def connect_sqlite(path: str | Path = ":memory:", *, check_same_thread: bool = True) -> sqlite3.Connection:
  conn = sqlite3.connect(path, check_same_thread=check_same_thread)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys=ON")
  return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
  conn.executescript(SCHEMA)
