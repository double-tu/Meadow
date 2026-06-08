"""Append-only run and sidechain transcript store."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Literal

from agent_kernel.domain.base import new_id, utc_now


TranscriptEntryType = Literal["user", "assistant", "tool", "system", "metadata", "task_notification"]


@dataclass(slots=True)
class TranscriptEntry:
  entry_type: TranscriptEntryType
  run_id: str
  content: dict[str, Any]
  entry_id: str = field(default_factory=lambda: new_id("transcript_entry"))
  parent_entry_id: str | None = None
  agent_id: str | None = None
  sidechain: bool = False
  timestamp: str = field(default_factory=lambda: utc_now().isoformat())

  def to_json_line(self) -> str:
    return json.dumps(
      {
        "entry_id": self.entry_id,
        "entry_type": self.entry_type,
        "run_id": self.run_id,
        "parent_entry_id": self.parent_entry_id,
        "agent_id": self.agent_id,
        "sidechain": self.sidechain,
        "timestamp": self.timestamp,
        "content": self.content,
      },
      ensure_ascii=False,
      sort_keys=True,
    )

  @classmethod
  def from_dict(cls, value: dict[str, Any]) -> "TranscriptEntry":
    return cls(
      entry_id=str(value["entry_id"]),
      entry_type=value["entry_type"],
      run_id=str(value["run_id"]),
      parent_entry_id=value.get("parent_entry_id"),
      agent_id=value.get("agent_id"),
      sidechain=bool(value.get("sidechain", False)),
      timestamp=str(value.get("timestamp") or utc_now().isoformat()),
      content=value.get("content") if isinstance(value.get("content"), dict) else {"value": value.get("content")},
    )


class FileTranscriptStore:
  """Stores main transcripts and agent sidechains as append-only JSONL files."""

  def __init__(self, root: str | Path) -> None:
    self._root = Path(root)

  def append(self, entry: TranscriptEntry) -> TranscriptEntry:
    path = self.path_for(entry.run_id, entry.agent_id if entry.sidechain else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
      handle.write(entry.to_json_line())
      handle.write("\n")
    return entry

  def list_entries(self, run_id: str, agent_id: str | None = None) -> list[TranscriptEntry]:
    path = self.path_for(run_id, agent_id)
    if not path.exists():
      return []
    entries: list[TranscriptEntry] = []
    with path.open("r", encoding="utf-8") as handle:
      for line in handle:
        line = line.strip()
        if not line:
          continue
        entries.append(TranscriptEntry.from_dict(json.loads(line)))
    return entries

  def append_metadata_tail(self, run_id: str, metadata: dict[str, Any]) -> TranscriptEntry:
    return self.append(
      TranscriptEntry(
        entry_type="metadata",
        run_id=run_id,
        content={"metadata": metadata, "placement": "tail_refresh"},
      )
    )

  def path_for(self, run_id: str, agent_id: str | None = None) -> Path:
    if agent_id:
      return self._root / run_id / "sidechains" / f"{agent_id}.jsonl"
    return self._root / f"{run_id}.jsonl"


@dataclass(slots=True)
class TranscriptChain:
  entries: list[TranscriptEntry]
  metadata: dict[str, Any] = field(default_factory=dict)


class TranscriptResumeService:
  """Rebuilds a linear chain and restores tail metadata."""

  def __init__(self, store: FileTranscriptStore) -> None:
    self._store = store

  def load(self, run_id: str, agent_id: str | None = None) -> TranscriptChain:
    entries = self._store.list_entries(run_id, agent_id)
    metadata: dict[str, Any] = {}
    transcript_entries: list[TranscriptEntry] = []
    seen: set[str] = set()
    for entry in entries:
      if entry.entry_type == "metadata":
        data = entry.content.get("metadata")
        if isinstance(data, dict):
          metadata.update(data)
        continue
      if entry.entry_id in seen and not entry.sidechain:
        continue
      seen.add(entry.entry_id)
      transcript_entries.append(entry)
    return TranscriptChain(entries=transcript_entries, metadata=metadata)
