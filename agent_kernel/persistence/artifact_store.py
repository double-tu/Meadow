"""Artifact metadata persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent_kernel.domain.base import utc_now
from agent_kernel.domain.identifiers import ArtifactRef


class ArtifactStore:
  def __init__(self, conn: sqlite3.Connection) -> None:
    self._conn = conn

  def save(self, ref: ArtifactRef, metadata: dict[str, Any] | None = None) -> None:
    self._conn.execute(
      """
      INSERT INTO artifacts (
        artifact_id,
        uri,
        media_type,
        version,
        checksum,
        metadata_json,
        created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(artifact_id) DO UPDATE SET
        uri = excluded.uri,
        media_type = excluded.media_type,
        version = excluded.version,
        checksum = excluded.checksum,
        metadata_json = excluded.metadata_json
      """,
      (
        ref.artifact_id,
        ref.uri,
        ref.media_type,
        ref.version,
        ref.checksum,
        json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        utc_now().isoformat(),
      ),
    )

  def get(self, artifact_id: str) -> ArtifactRef | None:
    row = self._conn.execute(
      """
      SELECT artifact_id, uri, media_type, version, checksum
      FROM artifacts
      WHERE artifact_id = ?
      """,
      (artifact_id,),
    ).fetchone()
    if row is None:
      return None
    return ArtifactRef(
      artifact_id=row["artifact_id"],
      uri=row["uri"],
      media_type=row["media_type"],
      version=row["version"],
      checksum=row["checksum"],
    )

  def list_by_ids(self, artifact_ids: list[str]) -> list[ArtifactRef]:
    return [
      ref
      for artifact_id in artifact_ids
      if (ref := self.get(artifact_id)) is not None
    ]

