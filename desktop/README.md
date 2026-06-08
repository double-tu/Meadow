# Meadow Desktop Shell

API-first desktop shell for the Meadow Python kernel. The shell defaults to a Chinese daily chat workspace and keeps runtime logic inside the Python API.

This shell deliberately keeps UI and kernel logic separate:

- The Python kernel owns runtime, agents, policy, MCP, schedules, and control backends.
- The desktop shell calls the HTTP/SSE API exposed by `agent_kernel.hosts.http`.
- Tauri is only the native wrapper. The React/Vite UI can also run in a browser for development.

## Run Locally

Install frontend dependencies once:

```bash
npm --prefix desktop install
```

Start both the Meadow HTTP API and the Vite UI:

```bash
python3 scripts/meadow_desktop.py start
```

Common service commands:

```bash
python3 scripts/meadow_desktop.py status
python3 scripts/meadow_desktop.py restart
python3 scripts/meadow_desktop.py stop
python3 scripts/meadow_desktop.py logs
```

The script defaults to:

```text
API: http://127.0.0.1:8083
UI:  http://127.0.0.1:4180
```

Override ports when needed with `--api-port` and `--ui-port`. The script also passes `VITE_MEADOW_API_URL` to the UI so the shell connects to the managed API by default.

## Current Views

- 日常对话: persistent chat sessions backed by Meadow task runs, with send/pause/retry/clear controls.
- 节点流转 / 工作流: run/workspace node cards and reserved workflow/sub-workflow review surfaces.
- 配置中心: structured model provider/model/capability/agent binding preview plus agent policy preview.
- Skill 管理 / 审批 / MCP / 任务 / 控制台 / 事件流: modular React feature entries over the existing HTTP API.

## Tauri Wrapper

The `src-tauri` directory is a minimal native wrapper around the Vite UI. It is intentionally thin and does not embed Meadow runtime code. A future sidecar launcher can be added without changing the UI API client.

Build the web UI:

```bash
npm --prefix desktop run build
```
