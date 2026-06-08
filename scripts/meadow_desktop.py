#!/usr/bin/env python3
"""Manage Meadow desktop API and Vite UI dev services."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_DIR = ROOT / "desktop"
DEFAULT_STATE_DIR = Path(os.environ.get("MEADOW_DESKTOP_STATE_DIR", "/tmp/meadow-desktop"))
DEFAULT_DB_PATH = os.environ.get("MEADOW_DESKTOP_DB", "/tmp/meadow-desktop.sqlite")
DEFAULT_API_HOST = os.environ.get("MEADOW_API_HOST", "127.0.0.1")
DEFAULT_UI_HOST = os.environ.get("MEADOW_UI_HOST", "127.0.0.1")
DEFAULT_API_PORT = int(os.environ.get("MEADOW_API_PORT", "8083"))
DEFAULT_UI_PORT = int(os.environ.get("MEADOW_UI_PORT", "4180"))
DEFAULT_BROWSER_LINK_URL = os.environ.get("MEADOW_BROWSER_LINK_URL", "http://127.0.0.1:18766/link")


def main() -> int:
  parser = argparse.ArgumentParser(prog="meadow-desktop", description="Manage Meadow desktop API and UI services.")
  parser.add_argument("command", choices=["start", "stop", "restart", "status", "logs"])
  parser.add_argument("--api-host", default=DEFAULT_API_HOST)
  parser.add_argument("--ui-host", default=DEFAULT_UI_HOST)
  parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
  parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT)
  parser.add_argument("--db", default=DEFAULT_DB_PATH)
  parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
  parser.add_argument("--browser-link-url", default=DEFAULT_BROWSER_LINK_URL)
  parser.add_argument("--no-browser-control", action="store_true", help="Start API without the browser-link control backend.")
  parser.add_argument("--no-ui", action="store_true", help="Only manage the Python API service.")
  parser.add_argument("--no-api", action="store_true", help="Only manage the Vite UI service.")
  parser.add_argument("--replace", action="store_true", help="Stop listeners on the configured ports before starting.")
  parser.add_argument("--tail", type=int, default=80, help="Lines to print for logs command.")
  args = parser.parse_args()

  args.state_dir.mkdir(parents=True, exist_ok=True)
  if args.command == "start":
    return start(args)
  if args.command == "stop":
    return stop(args)
  if args.command == "restart":
    stop(args)
    return start(args)
  if args.command == "status":
    return status(args)
  if args.command == "logs":
    return logs(args)
  return 1


def start(args: argparse.Namespace) -> int:
  if args.replace:
    stop(args)
  started = []
  if not args.no_api:
    if _port_pids(args.api_port):
      print(f"API port {args.api_port} is already in use. Use restart or --replace.")
    else:
      _start_api(args)
      started.append(f"API http://{args.api_host}:{args.api_port}")
  if not args.no_ui:
    if _port_pids(args.ui_port):
      print(f"UI port {args.ui_port} is already in use. Use restart or --replace.")
    else:
      _start_ui(args)
      started.append(f"UI  http://{args.ui_host}:{args.ui_port}")
  if started:
    print("Started Meadow desktop services:")
    for item in started:
      print(f"- {item}")
  return status(args)


def stop(args: argparse.Namespace) -> int:
  stopped: list[str] = []
  if not args.no_api:
    stopped.extend(_stop_service("api", args.state_dir, args.api_port))
  if not args.no_ui:
    stopped.extend(_stop_service("ui", args.state_dir, args.ui_port))
  if stopped:
    print("Stopped:")
    for item in stopped:
      print(f"- {item}")
  else:
    print("No matching Meadow desktop services were running.")
  return 0


def status(args: argparse.Namespace) -> int:
  print("Meadow desktop status:")
  if not args.no_api:
    _print_service_status("api", args.state_dir, args.api_port, f"http://{args.api_host}:{args.api_port}")
  if not args.no_ui:
    _print_service_status("ui", args.state_dir, args.ui_port, f"http://{args.ui_host}:{args.ui_port}")
  print(f"Logs: {args.state_dir}")
  return 0


def logs(args: argparse.Namespace) -> int:
  for name in ("api", "ui"):
    if (name == "api" and args.no_api) or (name == "ui" and args.no_ui):
      continue
    path = _log_path(args.state_dir, name)
    print(f"==> {path}")
    if not path.exists():
      print("(no log file)")
      continue
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.tail :]:
      print(line)
  return 0


def _start_api(args: argparse.Namespace) -> None:
  control_config = {
    "browser": {
      "enabled": not args.no_browser_control,
      "base_url": args.browser_link_url,
    },
    "desktop": {"enabled": False},
    "mobile": {"enabled": False},
  }
  code = (
    "import json, os;"
    "from agent_kernel.hosts.http import serve;"
    "serve("
    "os.environ['MEADOW_DESKTOP_DB'],"
    "host=os.environ.get('MEADOW_API_HOST', '127.0.0.1'),"
    "port=int(os.environ.get('MEADOW_API_PORT', '8083')),"
    "control_config=json.loads(os.environ.get('MEADOW_CONTROL_CONFIG', '{}'))"
    ")"
  )
  env = os.environ.copy()
  env.update(
    {
      "MEADOW_DESKTOP_DB": args.db,
      "MEADOW_API_HOST": args.api_host,
      "MEADOW_API_PORT": str(args.api_port),
      "MEADOW_CONTROL_CONFIG": json.dumps(control_config),
    }
  )
  _spawn(
    "api",
    [sys.executable, "-c", code],
    cwd=ROOT,
    env=env,
    state_dir=args.state_dir,
  )


def _start_ui(args: argparse.Namespace) -> None:
  env = os.environ.copy()
  env["VITE_MEADOW_API_URL"] = f"http://{args.api_host}:{args.api_port}"
  _spawn(
    "ui",
    ["npx", "vite", "--config", "ui/vite.config.ts", "--host", args.ui_host, "--port", str(args.ui_port)],
    cwd=DESKTOP_DIR,
    env=env,
    state_dir=args.state_dir,
  )


def _spawn(name: str, command: list[str], *, cwd: Path, env: dict[str, str], state_dir: Path) -> None:
  log_file = _log_path(state_dir, name).open("ab")
  process = subprocess.Popen(
    command,
    cwd=str(cwd),
    env=env,
    stdout=log_file,
    stderr=subprocess.STDOUT,
    start_new_session=True,
  )
  _pid_path(state_dir, name).write_text(str(process.pid), encoding="utf-8")
  time.sleep(0.4)
  if process.poll() is not None:
    log_file.close()
    raise SystemExit(f"{name} failed to start. Check {_log_path(state_dir, name)}")


def _stop_service(name: str, state_dir: Path, port: int) -> list[str]:
  stopped: list[str] = []
  pid_file = _pid_path(state_dir, name)
  pids: set[int] = set()
  if pid_file.exists():
    try:
      pids.add(int(pid_file.read_text(encoding="utf-8").strip()))
    except ValueError:
      pass
  pids.update(_port_pids(port))
  for pid in sorted(pids):
    if _terminate_pid(pid):
      stopped.append(f"{name} pid {pid}")
  pid_file.unlink(missing_ok=True)
  return stopped


def _terminate_pid(pid: int) -> bool:
  if not _is_running(pid):
    return False
  try:
    os.kill(pid, signal.SIGTERM)
  except PermissionError:
    print(f"Permission denied while stopping pid {pid}. Run the command with sufficient privileges.")
    return False
  except ProcessLookupError:
    return False
  for _ in range(20):
    if not _is_running(pid):
      return True
    time.sleep(0.1)
  try:
    os.kill(pid, signal.SIGKILL)
  except PermissionError:
    print(f"Permission denied while killing pid {pid}.")
    return False
  except ProcessLookupError:
    return True
  return True


def _is_running(pid: int) -> bool:
  try:
    os.kill(pid, 0)
  except ProcessLookupError:
    return False
  except PermissionError:
    return True
  return True


def _print_service_status(name: str, state_dir: Path, port: int, url: str) -> None:
  pid = _read_pid(_pid_path(state_dir, name))
  port_pids = _port_pids(port)
  running = (pid is not None and _is_running(pid)) or bool(port_pids)
  pids = sorted(set(([pid] if pid is not None else []) + port_pids))
  suffix = f" pid={','.join(str(item) for item in pids)}" if pids else ""
  state = "running" if running else "stopped"
  print(f"- {name}: {state} {url}{suffix}")


def _read_pid(path: Path) -> int | None:
  if not path.exists():
    return None
  try:
    return int(path.read_text(encoding="utf-8").strip())
  except ValueError:
    return None


def _port_pids(port: int) -> list[int]:
  try:
    result = subprocess.run(
      ["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"],
      check=False,
      text=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.DEVNULL,
    )
  except FileNotFoundError:
    return []
  return _parse_pids(result.stdout.splitlines())


def _parse_pids(lines: Iterable[str]) -> list[int]:
  pids: list[int] = []
  for line in lines:
    try:
      pids.append(int(line.strip()))
    except ValueError:
      continue
  return pids


def _pid_path(state_dir: Path, name: str) -> Path:
  return state_dir / f"{name}.pid"


def _log_path(state_dir: Path, name: str) -> Path:
  return state_dir / f"{name}.log"


if __name__ == "__main__":
  raise SystemExit(main())
