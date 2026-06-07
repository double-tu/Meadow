"""Generic JSONL shim for product CLI agent connectors."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def main(argv: list[str] | None = None) -> int:
  args = _build_parser().parse_args(argv)
  for line in sys.stdin:
    frame = json.loads(line)
    frame_type = frame.get("type")
    if frame_type == "start":
      _write({"type": "started", "session_id": frame.get("session_id"), "product": args.product})
      continue
    if frame_type == "message":
      _write(_run_turn(args, frame))
      continue
    if frame_type == "stop":
      return 0
    _write(
      {
        "type": "turn",
        "turn_id": f"error_{frame.get('message_id', 'unknown')}",
        "session_id": frame.get("session_id"),
        "output": {"ok": False, "error": {"type": "unsupported_frame", "frame_type": frame_type}},
        "completed": True,
      }
    )
  return 0


def _build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="agent-kernel-cli-shim")
  parser.add_argument("--product", required=True)
  parser.add_argument("--executable", required=True)
  parser.add_argument("--arg", action="append", default=[])
  parser.add_argument("--args-json", default=None)
  parser.add_argument("--prompt-mode", choices=["stdin", "argument", "json_stdin"], default="stdin")
  parser.add_argument("--prompt-argument", default=None)
  parser.add_argument("--output-format", choices=["text", "json"], default="text")
  parser.add_argument("--timeout-seconds", type=float, default=120.0)
  return parser


def _run_turn(args: argparse.Namespace, frame: dict[str, Any]) -> dict[str, Any]:
  prompt = _prompt_from_content(frame.get("content", {}))
  command = [args.executable, *_default_args(args)]
  stdin_payload: str | None = None
  if args.prompt_mode == "stdin":
    stdin_payload = prompt
  elif args.prompt_mode == "json_stdin":
    stdin_payload = json.dumps(
      {
        "session_id": frame.get("session_id"),
        "message_id": frame.get("message_id"),
        "content": frame.get("content", {}),
        "prompt": prompt,
      },
      ensure_ascii=False,
      sort_keys=True,
    )
  else:
    if args.prompt_argument:
      command.extend([args.prompt_argument, prompt])
    else:
      command.append(prompt)
  try:
    completed = subprocess.run(
      command,
      input=stdin_payload,
      text=True,
      capture_output=True,
      timeout=args.timeout_seconds,
      check=False,
    )
  except subprocess.TimeoutExpired:
    return _turn(
      frame,
      args.product,
      ok=False,
      error={"type": "product_cli_timeout", "timeout_seconds": args.timeout_seconds},
    )
  except OSError as exc:
    return _turn(
      frame,
      args.product,
      ok=False,
      error={"type": "product_cli_exec_failed", "message": str(exc)},
    )
  output: dict[str, Any] = {
    "ok": completed.returncode == 0,
    "product": args.product,
    "returncode": completed.returncode,
    "stdout": completed.stdout,
    "stderr": completed.stderr,
  }
  if args.output_format == "json" and completed.stdout.strip():
    try:
      parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
      parsed = None
    if isinstance(parsed, dict):
      output["json"] = parsed
  error = None
  if completed.returncode != 0:
    error = {
      "type": "product_cli_failed",
      "returncode": completed.returncode,
      "stderr": completed.stderr,
    }
  return _turn(frame, args.product, ok=completed.returncode == 0, output=output, error=error)


def _turn(
  frame: dict[str, Any],
  product: str,
  ok: bool,
  output: dict[str, Any] | None = None,
  error: dict[str, Any] | None = None,
) -> dict[str, Any]:
  return {
    "type": "turn",
    "turn_id": f"{product}_{frame.get('message_id', 'message')}",
    "session_id": frame.get("session_id"),
    "output": output or {"ok": ok, "product": product, "error": error},
    "completed": True,
  }


def _prompt_from_content(content: object) -> str:
  if isinstance(content, dict):
    for key in ("prompt", "task", "text", "content"):
      value = content.get(key)
      if isinstance(value, str):
        return value
    return json.dumps(content, ensure_ascii=False, sort_keys=True)
  if isinstance(content, str):
    return content
  return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _default_args(args: argparse.Namespace) -> list[str]:
  values = list(args.arg or [])
  if args.args_json is None:
    return values
  parsed = json.loads(args.args_json)
  if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
    raise ValueError("--args-json must decode to a list of strings.")
  return [*values, *parsed]


def _write(payload: dict[str, Any]) -> None:
  print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
  raise SystemExit(main())
