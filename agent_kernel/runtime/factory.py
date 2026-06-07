"""Runtime factory helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from agent_kernel.persistence import UnitOfWork


def unit_of_work_factory(conn: sqlite3.Connection) -> Callable[[], UnitOfWork]:
  return lambda: UnitOfWork(conn)

