const DEFAULT_API_URL = "http://127.0.0.1:8080";

const state = {
  apiUrl: localStorage.getItem("meadow.apiUrl") || DEFAULT_API_URL,
  view: "chat",
  chatSessionId: null,
  configSection: "llm",
  sending: false,
  chatError: "",
};

const viewMeta = {
  chat: ["日常对话", "持续沟通、组合任务、沉淀 Skill。"],
  workspaces: ["工作区", "运行、Agent、Artifact、审批与工具调用的聚合视图。"],
  skills: ["Skill 管理", "统一创建、启用、弃用和查看可复用能力。"],
  config: ["配置中心", "热更新 UI、大模型、Agent、MCP、控制能力等配置。"],
  approvals: ["审批", "集中处理工具调用和高风险动作。"],
  mcp: ["MCP", "统一管理 MCP Server 与 Agent 绑定。"],
  schedules: ["计划任务", "管理一次性、周期和 cron 任务。"],
  control: ["控制台", "浏览器、桌面和移动控制面。"],
  events: ["事件流", "按游标查看 runtime 事件。"],
};

const els = {
  view: document.querySelector("#view"),
  title: document.querySelector("#view-title"),
  subtitle: document.querySelector("#view-subtitle"),
  status: document.querySelector("#status-strip"),
  hostInput: document.querySelector("#host-url"),
  hostForm: document.querySelector("#host-form"),
  nav: Array.from(document.querySelectorAll(".nav-item")),
};

els.hostInput.value = state.apiUrl;

els.hostForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.apiUrl = normalizeApiUrl(els.hostInput.value);
  localStorage.setItem("meadow.apiUrl", state.apiUrl);
  render();
});

for (const item of els.nav) {
  item.addEventListener("click", () => {
    state.view = item.dataset.view;
    render();
  });
}

render();

async function render() {
  for (const item of els.nav) item.classList.toggle("active", item.dataset.view === state.view);
  const [title, subtitle] = viewMeta[state.view];
  els.title.textContent = title;
  els.subtitle.textContent = subtitle;
  els.view.innerHTML = "";
  renderStatus();
  try {
    if (state.view === "chat") await renderChat();
    if (state.view === "workspaces") await renderWorkspaces();
    if (state.view === "skills") await renderSkills();
    if (state.view === "config") await renderConfig();
    if (state.view === "approvals") await renderApprovals();
    if (state.view === "events") await renderEvents();
    if (state.view === "mcp") await renderMcp();
    if (state.view === "schedules") await renderSchedules();
    if (state.view === "control") await renderControl();
  } catch (error) {
    els.view.append(errorBlock(error));
  }
}

function renderStatus() {
  els.status.innerHTML = "";
  els.status.append(pill(`API ${state.apiUrl}`), pill("语言 zh-CN"), pill("模式 API-first"));
}

async function renderChat() {
  let sessions = (await apiGet("/chat/sessions")).chat_sessions || [];
  if (!sessions.length) {
    const created = await apiPost("/chat/sessions", { title: "新的对话" });
    sessions = [created.chat_session];
  }
  if (!state.chatSessionId || !sessions.some((item) => item.session_id === state.chatSessionId)) {
    state.chatSessionId = sessions[0].session_id;
  }
  const active = sessions.find((item) => item.session_id === state.chatSessionId);
  const messages = (await apiGet(`/chat/sessions/${encodeURIComponent(state.chatSessionId)}/messages`)).messages || [];
  const skills = (await apiGet("/skills?active_only=true")).skills || [];
  const mcp = (await apiGet("/mcp-servers")).mcp_servers || [];
  const approvals = (await apiGet("/approvals")).approvals || [];
  const workspaces = (await apiGet("/workspaces")).workspaces || [];

  const layout = div("chat-layout");
  layout.append(renderChatSessions(sessions), renderChatMain(active, messages, skills), renderChatInspector(skills, approvals, workspaces, mcp));
  els.view.append(layout);
  requestAnimationFrame(() => {
    const box = document.querySelector(".messages");
    if (box) box.scrollTop = box.scrollHeight;
  });
}

function renderChatSessions(sessions) {
  const panel = div("panel");
  const header = div("panel-header");
  const h = document.createElement("h2");
  h.textContent = "对话";
  header.append(h, button("新建", async () => {
    const created = await apiPost("/chat/sessions", { title: "新的对话" });
    state.chatSessionId = created.chat_session.session_id;
    await render();
  }));
  const body = div("panel-body");
  const list = div("session-list");
  for (const session of sessions) {
    const item = button("", async () => {
      state.chatSessionId = session.session_id;
      await render();
    }, `session-item ${session.session_id === state.chatSessionId ? "active" : ""}`);
    const title = document.createElement("strong");
    title.textContent = session.title || "新的对话";
    const sub = document.createElement("span");
    sub.textContent = `${session.message_count || 0} 条消息`;
    item.append(title, sub);
    list.append(item);
  }
  body.append(list);
  panel.append(header, body);
  return panel;
}

function renderChatMain(session, messages, skills) {
  const panel = div("panel chat-panel");
  const header = div("panel-header");
  const h = document.createElement("h2");
  h.textContent = session?.title || "新的对话";
  header.append(h, div("row-actions",
    button("暂停", async () => {
      await apiPost(`/chat/sessions/${encodeURIComponent(state.chatSessionId)}/pause`, { reason: "用户在桌面端暂停" });
      await render();
    }),
    button("重试", async () => {
      await apiPost(`/chat/sessions/${encodeURIComponent(state.chatSessionId)}/retry`, {});
      await render();
    }),
    button("清空上下文", async () => {
      await apiDelete(`/chat/sessions/${encodeURIComponent(state.chatSessionId)}/messages`);
      await render();
    }, "danger"),
  ));
  const box = div("messages");
  if (state.chatError) {
    box.append(errorBlock(state.chatError));
  }
  if (!messages.length) {
    box.append(empty("直接输入日常问题、任务或要组合的工作流。"));
  } else {
    for (const message of messages) box.append(messageBubble(message));
  }
  if (state.sending) {
    box.append(div("message assistant", div("message-meta", "Meadow"), document.createTextNode("执行中... 正在创建任务并等待结果。")));
  }
  const form = document.createElement("form");
  form.className = "chat-composer";
  const textareaNode = textarea("输入消息。Enter 发送，Shift+Enter 换行。", "");
  const send = document.createElement("button");
  send.className = "primary";
  send.textContent = state.sending ? "执行中" : "发送";
  send.disabled = state.sending;
  form.append(textareaNode, send);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await sendChatMessage(textareaNode, skills);
  });
  textareaNode.addEventListener("keydown", async (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      await sendChatMessage(textareaNode, skills);
    }
  });
  panel.append(header, box, form);
  return panel;
}

async function sendChatMessage(textareaNode, skills) {
  const content = textareaNode.value.trim();
  if (!content) return;
  textareaNode.value = "";
  state.sending = true;
  state.chatError = "";
  await render();
  try {
    await apiPost(`/chat/sessions/${encodeURIComponent(state.chatSessionId)}/messages`, {
      content,
      selected_skill_ids: skills.map((skill) => skill.skill_id),
      mode: "agent",
    });
  } catch (error) {
    state.chatError = error.message || String(error);
  } finally {
    state.sending = false;
  }
  await render();
}

function renderChatInspector(skills, approvals, workspaces, mcp) {
  const panel = div("panel");
  const header = div("panel-header");
  const h = document.createElement("h2");
  h.textContent = "上下文";
  header.append(h);
  const body = div("panel-body inspector-list");
  body.append(miniCard("可用 Skill", skills.length ? skills.map((item) => item.name).join("、") : "暂无 active skill"));
  body.append(miniCard("MCP 工具", mcp.length ? mcp.map((item) => item.name).join("、") : "暂无 MCP server"));
  body.append(miniCard("待审批", approvals.length ? `${approvals.length} 个待处理` : "无"));
  body.append(miniCard("工作区", workspaces.length ? `${workspaces.length} 个运行视图` : "暂无运行"));
  body.append(miniCard("Agent/工作流", "每条消息进入 Meadow task，可沉淀 Skill、观察事件、继续扩展为子 Agent 委派和 workflow 组合。"));
  panel.append(header, body);
  return panel;
}

function messageBubble(message) {
  const node = div(`message ${message.role}`);
  const meta = div("message-meta");
  meta.textContent = message.role === "user" ? "你" : `Meadow${message.run_id ? ` · ${message.run_id}` : ""}`;
  const content = document.createElement("div");
  content.textContent = message.content;
  node.append(meta, content);
  return node;
}

async function renderSkills() {
  const data = await apiGet("/skills");
  const toolbar = div("toolbar");
  const name = input("Skill 名称", "代码评审");
  const description = input("描述", "评审代码风险和测试缺口");
  const when = input("何时使用", "需要 review 或合并前");
  const instructions = textarea("执行说明", "列出主要风险、证据、建议和缺失测试。");
  toolbar.append(name, description, when, instructions, button("创建并启用", async () => {
    await apiPost("/skills", {
      name: name.value.trim(),
      description: description.value.trim(),
      when_to_use: when.value.trim(),
      instructions: instructions.value.trim(),
      status: "active",
    });
    await render();
  }, "primary"));
  els.view.append(toolbar);

  const grid = div("grid");
  for (const skill of data.skills || []) {
    const node = card(skill.name, [
      ["状态", skill.status],
      ["模式", skill.execution_mode],
      ["描述", skill.description || "无"],
      ["何时使用", skill.when_to_use || "无"],
    ]);
    node.append(div("row-actions",
      button("启用", async () => {
        await apiPost(`/skills/${encodeURIComponent(skill.skill_id)}/activate`, {});
        await render();
      }),
      button("弃用", async () => {
        await apiPost(`/skills/${encodeURIComponent(skill.skill_id)}/deprecate`, {});
        await render();
      }, "danger"),
    ));
    grid.append(node);
  }
  els.view.append(grid.children.length ? grid : empty("暂无 Skill。"));
}

async function renderConfig() {
  const data = await apiGet("/config");
  const sections = data.config_sections || [];
  if (!sections.some((item) => item.section === state.configSection)) {
    state.configSection = sections[0]?.section || "llm";
  }
  const current = (await apiGet(`/config/${encodeURIComponent(state.configSection)}`)).config_section;
  const layout = div("config-layout");

  const listPanel = div("panel");
  const listHeader = div("panel-header");
  const listTitle = document.createElement("h2");
  listTitle.textContent = "配置分区";
  listHeader.append(listTitle);
  const listBody = div("panel-body session-list");
  for (const section of sections) {
    const item = button(section.section, async () => {
      state.configSection = section.section;
      await render();
    }, `session-item ${section.section === state.configSection ? "active" : ""}`);
    listBody.append(item);
  }
  listPanel.append(listHeader, listBody);

  const editor = div("panel config-editor");
  const editorHeader = div("panel-header");
  const editorTitle = document.createElement("h2");
  editorTitle.textContent = `配置：${current.section}`;
  editorHeader.append(editorTitle, pill("热更新"));
  const body = div("panel-body config-editor");
  const jsonEditor = textarea("JSON 配置", JSON.stringify(current.data, null, 2));
  body.append(jsonEditor, div("row-actions", button("保存", async () => {
    await apiPatch(`/config/${encodeURIComponent(current.section)}`, { data: JSON.parse(jsonEditor.value), merge: false });
    await render();
  }, "primary")));
  editor.append(editorHeader, body);
  layout.append(listPanel, editor);
  els.view.append(layout);
}

async function renderWorkspaces() {
  const data = await apiGet("/workspaces");
  const toolbar = div("toolbar");
  const title = input("任务标题", "检查当前项目状态");
  const runId = input("run_id 可选", "");
  toolbar.append(title, runId, button("创建任务", async () => {
    await apiPost("/tasks", { title: title.value.trim() || "桌面任务", run_id: runId.value.trim() || undefined });
    await render();
  }, "primary"));
  els.view.append(toolbar);
  const grid = div("grid");
  for (const workspace of data.workspaces || []) {
    grid.append(card(workspace.title, [
      ["Workspace", workspace.workspace_id],
      ["Runs", workspace.run_ids?.join(", ") || "无"],
      ["Artifacts", count(workspace.artifact_ids)],
      ["待审批", count(workspace.pending_approval_ids)],
      ["活跃工具", count(workspace.active_tool_call_ids)],
      ["Delegations", count(workspace.delegation_task_ids)],
    ]));
  }
  els.view.append(grid.children.length ? grid : empty("暂无工作区。"));
}

async function renderApprovals() {
  const data = await apiGet("/approvals");
  const grid = div("grid");
  for (const approval of data.approvals || []) {
    const node = card(approval.reason || approval.approval_id, [
      ["Approval", approval.approval_id],
      ["Run", approval.run_id],
      ["Target", `${approval.target_type}:${approval.target_id}`],
      ["状态", approval.status],
    ], approval.requested_payload);
    node.append(div("row-actions",
      button("批准", async () => {
        await apiPost(`/approvals/${encodeURIComponent(approval.approval_id)}/approve`, { ttl_seconds: 300 });
        await render();
      }, "primary"),
      button("拒绝", async () => {
        await apiPost(`/approvals/${encodeURIComponent(approval.approval_id)}/reject`, {});
        await render();
      }, "danger"),
    ));
    grid.append(node);
  }
  els.view.append(grid.children.length ? grid : empty("暂无待审批项。"));
}

async function renderEvents() {
  const toolbar = div("toolbar");
  const runId = input("run_id 可选", "");
  const after = input("after_event_id 可选", "");
  toolbar.append(runId, after, button("加载", async () => renderEventsList(runId.value.trim(), after.value.trim())));
  els.view.append(toolbar);
  await renderEventsList("", "");
}

async function renderEventsList(runId, afterEventId) {
  const old = document.querySelector("#events-list");
  if (old) old.remove();
  const query = new URLSearchParams();
  if (runId) query.set("run_id", runId);
  if (afterEventId) query.set("after_event_id", afterEventId);
  const events = await apiNdjson(`/events${query.toString() ? `?${query}` : ""}`);
  const list = div("grid");
  list.id = "events-list";
  for (const event of events.slice(-100)) {
    list.append(card(event.event_type, [["Event", event.event_id], ["Run", event.run_id || "无"], ["时间", event.emitted_at]], event.payload));
  }
  els.view.append(list.children.length ? list : empty("暂无事件。"));
}

async function renderMcp() {
  const data = await apiGet("/mcp-servers");
  const toolbar = div("toolbar");
  const name = input("name", "local");
  const command = input("stdio command", "python3");
  const args = input("args JSON array", "[]");
  toolbar.append(name, command, args, button("保存 stdio", async () => {
    await apiPost("/mcp-servers", {
      name: name.value.trim(),
      enabled: true,
      transport: { type: "stdio", command: command.value.trim(), args: JSON.parse(args.value || "[]") },
    });
    await render();
  }, "primary"));
  els.view.append(toolbar);
  const grid = div("grid");
  for (const server of data.mcp_servers || []) {
    const node = card(server.name, [["启用", String(server.enabled)], ["Transport", server.transport?.type || "unknown"], ["Agent types", server.agent_types?.join(", ") || "全部"]]);
    node.append(div("row-actions", button("删除", async () => {
      await apiDelete(`/mcp-servers/${encodeURIComponent(server.name)}`);
      await render();
    }, "danger")));
    grid.append(node);
  }
  els.view.append(grid.children.length ? grid : empty("暂无 MCP Server。"));
}

async function renderSchedules() {
  const data = await apiGet("/scheduled-tasks");
  const toolbar = div("toolbar");
  const name = input("名称", "每日检查");
  const kind = select(["at", "every", "cron"]);
  const value = input("调度值", "1h");
  const payload = textarea("payload JSON", JSON.stringify({ title: "计划任务" }, null, 2));
  toolbar.append(name, kind, value, payload, button("创建", async () => {
    await apiPost("/scheduled-tasks", { name: name.value.trim(), schedule_kind: kind.value, schedule_value: value.value.trim(), payload: JSON.parse(payload.value || "{}") });
    await render();
  }, "primary"), button("运行到期任务", async () => {
    await apiPost("/scheduled-tasks/run-due", {});
    await render();
  }));
  els.view.append(toolbar);
  const grid = div("grid");
  for (const task of data.scheduled_tasks || []) {
    const node = card(task.name, [["Task", task.task_id], ["Schedule", `${task.schedule_kind} ${task.schedule_value}`], ["启用", String(task.enabled)], ["下次运行", task.next_run_at || "无"], ["触发次数", String(task.trigger_count)]]);
    node.append(div("row-actions",
      button(task.enabled ? "停用" : "启用", async () => {
        await apiPatch(`/scheduled-tasks/${encodeURIComponent(task.task_id)}`, { enabled: !task.enabled });
        await render();
      }),
      button("删除", async () => {
        await apiDelete(`/scheduled-tasks/${encodeURIComponent(task.task_id)}`);
        await render();
      }, "danger"),
    ));
    grid.append(node);
  }
  els.view.append(grid.children.length ? grid : empty("暂无计划任务。"));
}

async function renderControl() {
  const health = await apiGet("/control/health");
  const targets = await apiGet("/control/targets");
  els.view.append(card("健康状态", [["Overall", String(health.ok)]], health.checks));
  const toolbar = div("toolbar");
  const kind = select(["browser", "desktop", "mobile"]);
  const action = select(["inspect", "screenshot", "dump_ui", "execute_js", "navigate", "click", "key", "type_text", "tap"]);
  const targetId = input("target id 可选", "");
  const payload = textarea("payload JSON", "{}");
  toolbar.append(kind, action, targetId, payload, button("执行", async () => {
    const result = await apiPost("/control/commands", { target_kind: kind.value, action: action.value, target_id: targetId.value.trim() || undefined, payload: JSON.parse(payload.value || "{}") });
    alert(JSON.stringify(result.result, null, 2));
  }, "primary"));
  els.view.append(toolbar);
  const grid = div("grid");
  for (const target of targets.targets || []) {
    grid.append(card(target.label || target.target_id, [["Target", target.target_id], ["Kind", target.kind]], target.metadata));
  }
  els.view.append(grid.children.length ? grid : empty("暂无控制目标。"));
}

async function apiGet(path) {
  return apiJson("GET", path);
}

async function apiPost(path, body) {
  return apiJson("POST", path, body);
}

async function apiPatch(path, body) {
  return apiJson("PATCH", path, body);
}

async function apiDelete(path) {
  return apiJson("DELETE", path);
}

async function apiJson(method, path, body) {
  const response = await fetch(`${state.apiUrl}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error || `${method} ${path} failed`);
  return data;
}

async function apiNdjson(path) {
  const response = await fetch(`${state.apiUrl}${path}`);
  if (!response.ok) throw new Error(`GET ${path} failed`);
  const text = await response.text();
  return text.split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

function normalizeApiUrl(value) {
  return (value || DEFAULT_API_URL).trim().replace(/\/+$/, "");
}

function card(title, rows, details) {
  const node = div("card");
  const heading = document.createElement("h2");
  heading.textContent = title;
  const meta = div("meta");
  for (const [key, value] of rows) {
    const row = document.createElement("div");
    row.innerHTML = `<strong>${escapeHtml(key)}:</strong> ${escapeHtml(String(value))}`;
    meta.append(row);
  }
  node.append(heading, meta);
  if (details && Object.keys(details).length) {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(details, null, 2);
    node.append(pre);
  }
  return node;
}

function miniCard(title, text) {
  const node = div("mini-card");
  const h = document.createElement("strong");
  h.textContent = title;
  const body = document.createElement("span");
  body.textContent = text;
  node.append(h, body);
  return node;
}

function div(className, ...children) {
  const node = document.createElement("div");
  if (className) node.className = className;
  node.append(...children.filter(Boolean));
  return node;
}

function button(label, onClick, className = "") {
  const node = document.createElement("button");
  node.type = "button";
  node.textContent = label;
  if (className) node.className = className;
  node.addEventListener("click", onClick);
  return node;
}

function input(placeholder, value) {
  const node = document.createElement("input");
  node.placeholder = placeholder;
  node.value = value;
  return node;
}

function textarea(placeholder, value) {
  const node = document.createElement("textarea");
  node.placeholder = placeholder;
  node.value = value;
  return node;
}

function select(values) {
  const node = document.createElement("select");
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    node.append(option);
  }
  return node;
}

function pill(text) {
  const node = div("pill");
  node.textContent = text;
  return node;
}

function empty(text) {
  const node = div("empty");
  node.textContent = text;
  return node;
}

function errorBlock(error) {
  const node = div("error");
  node.textContent = error.message || String(error);
  return node;
}

function count(items) {
  return String(Array.isArray(items) ? items.length : 0);
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#039;",
  }[char]));
}
