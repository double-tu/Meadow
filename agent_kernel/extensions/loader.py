"""Extension manifest loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_kernel.domain.extension import ExtensionManifest


class ExtensionManifestLoader:
  def load_dict(self, data: dict[str, Any]) -> ExtensionManifest:
    return ExtensionManifest.from_dict(data)

  def load_json(self, path: str | Path) -> ExtensionManifest:
    with Path(path).open("r", encoding="utf-8") as handle:
      return self.load_dict(json.load(handle))

