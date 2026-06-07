"""Capability adapter implementations."""

from agent_kernel.capabilities.adapters.control import (
  ADBMobileBackend,
  CommandResult,
  ControlBackend,
  ControlCommand,
  ControlResult,
  ControlTarget,
  ControlWorkbench,
  DesktopUIDetector,
  FakeControlBackend,
  TMWebDriverHTTPBackend,
  UIAStyleDesktopDetector,
  Win32DesktopBackend,
)
from agent_kernel.capabilities.adapters.local import LocalToolExecutor
from agent_kernel.capabilities.adapters.mcp import (
  FakeMCPClient,
  MCPClient,
  MCPServerCommand,
  MCPToolBinding,
  MCPToolExecutor,
  StdioMCPClient,
)
from agent_kernel.capabilities.adapters.process import ProcessCommand, ProcessStreamEvent, ProcessToolExecutor
from agent_kernel.capabilities.adapters.workbench import (
  FakeWorkbenchClient,
  WorkbenchClient,
  WorkbenchCommand,
  WorkbenchResult,
)

__all__ = [
  "ADBMobileBackend",
  "CommandResult",
  "ControlBackend",
  "ControlCommand",
  "ControlResult",
  "ControlTarget",
  "ControlWorkbench",
  "DesktopUIDetector",
  "FakeMCPClient",
  "FakeControlBackend",
  "FakeWorkbenchClient",
  "LocalToolExecutor",
  "MCPClient",
  "MCPServerCommand",
  "MCPToolBinding",
  "MCPToolExecutor",
  "ProcessCommand",
  "ProcessStreamEvent",
  "ProcessToolExecutor",
  "StdioMCPClient",
  "TMWebDriverHTTPBackend",
  "UIAStyleDesktopDetector",
  "Win32DesktopBackend",
  "WorkbenchClient",
  "WorkbenchCommand",
  "WorkbenchResult",
]
