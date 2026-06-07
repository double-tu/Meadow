# Agent Kernel

Python MVP implementation of the Agent Kernel architecture described in `python-code-architecture-design.md`.

## Current Scope

Implemented MVP areas:

- Durable runtime with SQLite event log, state, checkpoint, retry, dead letter, budget, circuit breaker, idempotency basics, stale-step recovery scanner, recovery consistency checks, and conservative repair.
- Workflow graph execution with function, tool, and agent node executors.
- Agent orchestration with sessions, mailbox, child agent spawn/await/cancel, and agent-as-workflow-node.
- Capability runtime with policy checks, approval flow, audit records, local/process tools, timeout, and tool-call cancel/kill control.
- Memory/context with working/artifact memory, context budget, sensitivity filtering, tool visibility pruning, large-memory artifact refs, and context ledger.
- Observability/replay with timeline, artifact inspect, cost ledger, exact/partial/recovery replay, and eval assertions.
- Extension manifest loader, contribution registry, and permission-to-grant mapping.
- Autonomous exploration MVP with strategy planning, attempts, verification, trace distillation, draft workflow templates, and skill evolution records.
- Multi-agent interaction fabric MVP with channels, messages, round-robin group chat, agent pools, taskboard basics, and observer findings.
- Workspace isolation interfaces with patch artifact review/merge workflow and fake backend for tests.
- Human intervention with event append, working-memory steering, and CLI `intervene`.
- Skill service and plan patch validation for controlled skill/workflow evolution.
- MCP, Workbench, and persistent agent connector protocol boundaries with fake implementations.
- Host DTOs for event stream, task workspace, and HTTP route planning.
- CLI host for sample run, inspect, replay, approve, reject, cancel, cancel/kill tool call, intervene, and llm-smoke.
- OpenAI-compatible LLM smoke command configured by environment variables or TOML/JSON config.

## Quick Start

Run tests:

```bash
python3 -m unittest discover -s tests
```

Run a sample workflow:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite sample-run --run-id run_demo --text hello
```

Inspect timeline:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite inspect run_demo
```

Replay:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/agent_kernel.sqlite replay run_demo
```

Run a real LLM smoke call with an OpenAI-compatible endpoint:

```bash
export AGENT_KERNEL_LLM_MODEL="gpt-4.1-mini"
export AGENT_KERNEL_LLM_API_KEY="your-api-key"
export AGENT_KERNEL_LLM_BASE_URL="https://api.openai.com/v1"
python3 -m agent_kernel.hosts.cli llm-smoke --prompt "Say hello in one sentence."
```

The environment loader also accepts `OPENAI_MODEL`, `OPENAI_API_KEY`, and `OPENAI_BASE_URL`.

Or use a config file:

```toml
[llm]
provider = "openai-compatible"
model = "gpt-4.1-mini"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 60
```

Then run:

```bash
export OPENAI_API_KEY="your-api-key"
python3 -m agent_kernel.hosts.cli --config agent-kernel.toml llm-smoke --prompt "Say hello."
```

If installed as a package, the console script is:

```bash
agent-kernel --db /tmp/agent_kernel.sqlite sample-run --run-id run_demo --text hello
```

## Engineering Notes

- Runtime facts are persisted through event/state/checkpoint stores.
- Large payloads should be stored as artifacts and referenced from events.
- All side-effecting capabilities go through `CapabilityRuntime` and `PolicyEngine`.
- `ReplayService` does not re-execute model/tool calls.
- Current CLI is a minimal host, not the final Web/Desktop workspace.
- This repository does not yet implement computer control, browser automation, GUI operation, or mobile-device control; it provides adapter boundaries for future integration.

## Known MVP Gaps

- HTTP server is not implemented; only route/stream/workspace DTOs exist.
- Tool-call cancel/kill only performs live process control inside the current runtime process; after restart, host commands persist control requests but cannot signal the original child process.
- Real persistent CLI agent connector is not implemented; only protocol and fake connector exist.
- Real Workbench/MCP adapters are not implemented; only contracts and fake clients exist.
- Recovery scanner can reschedule or dead-letter stale steps and conservatively repair missing/misaligned RunState checkpoint metadata, but complex artifact/event repair workflows are not implemented.
- Workspace isolation has protocol and fake-backed patch review workflow; real git worktree allocation and merge execution are not implemented.
- Advanced semantic memory, vector search, and long-term consolidation are not implemented.
- Advanced autonomous reflection and PlanPatch validation remain future work.
