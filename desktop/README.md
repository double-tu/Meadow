# Meadow Desktop Shell

API-first desktop shell for the Meadow Python kernel. The shell defaults to a Chinese daily chat workspace and keeps runtime logic inside the Python API.

This shell deliberately keeps UI and kernel logic separate:

- The Python kernel owns runtime, agents, policy, MCP, schedules, and control backends.
- The desktop shell calls the HTTP/SSE API exposed by `agent_kernel.hosts.http`.
- Tauri is only the native wrapper. The React/Vite UI can also run in a browser for development.

## Run Locally

Start the Meadow HTTP host in another terminal:

```bash
python3 -m agent_kernel.hosts.cli --db /tmp/meadow-desktop.sqlite http --host 127.0.0.1 --port 8080
```

Install frontend dependencies once:

```bash
npm --prefix desktop install
```

Serve the shell:

```bash
npm --prefix desktop run dev:web
```

Then set the API URL in the top-right field if needed. The default is:

```text
http://127.0.0.1:8080
```

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
