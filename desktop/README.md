# Meadow Desktop Shell

API-first desktop shell for the Meadow Python kernel. The shell defaults to a Chinese daily chat workspace and keeps runtime logic inside the Python API.

This shell deliberately keeps UI and kernel logic separate:

- The Python kernel owns runtime, agents, policy, MCP, schedules, and control backends.
- The desktop shell calls the HTTP/SSE API exposed by `agent_kernel.hosts.http`.
- Tauri is only the native wrapper. The static UI can also run in a browser for development.

## Run Locally

Start the Meadow HTTP host in another terminal:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/meadow-desktop.sqlite http --host 127.0.0.1 --port 8080
```

Serve the shell:

```bash
python3 -m http.server 4173 --directory desktop/static
```

Then set the API URL in the top-right field if needed. The default is:

```text
http://127.0.0.1:8080
```

## Current Views

- 日常对话: persistent chat sessions backed by Meadow task runs.
- Skill 管理: create, activate, and deprecate interpreted Skill cards.
- 配置中心: hot-update UI, LLM, agent, MCP, and control configuration sections.
- 工作区: aggregate runs, artifacts, approvals, delegations, and active tool calls.
- 审批 / MCP / 计划任务 / 控制台 / 事件流: operational views over the existing HTTP API.

## Tauri Wrapper

The `src-tauri` directory is a minimal native wrapper around the static UI. It is intentionally thin and does not embed Meadow runtime code. A future sidecar launcher can be added without changing the UI API client.
