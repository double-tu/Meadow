"""Capability adapter implementations."""

from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.capabilities.adapters.mcp import FakeMCPClient, MCPClient
from agent_kernel.capabilities.adapters.process import ProcessCommand, ProcessToolExecutor
from agent_kernel.capabilities.adapters.workbench import (
  FakeWorkbenchClient,
  WorkbenchClient,
  WorkbenchCommand,
  WorkbenchResult,
)

__all__ = [
  "FakeMCPClient",
  "FakeWorkbenchClient",
  "LocalToolExecutor",
  "MCPClient",
  "ProcessCommand",
  "ProcessToolExecutor",
  "WorkbenchClient",
  "WorkbenchCommand",
  "WorkbenchResult",
]
