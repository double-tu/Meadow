"""Audit sink abstractions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agent_kernel.persistence.audit_store import AuditRecord


class AuditSink(Protocol):
  def emit(self, record: AuditRecord) -> None:
    ...


class MemoryAuditSink:
  """In-memory sink for tests and embedded hosts."""

  def __init__(self) -> None:
    self.records: list[AuditRecord] = []

  def emit(self, record: AuditRecord) -> None:
    self.records.append(record)


@dataclass(slots=True)
class CompositeAuditSink:
  sinks: list[AuditSink] = field(default_factory=list)

  def emit(self, record: AuditRecord) -> None:
    for sink in self.sinks:
      sink.emit(record)

  def add(self, sink: AuditSink) -> None:
    self.sinks.append(sink)
