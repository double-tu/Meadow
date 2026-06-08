const DEFAULT_API_URL = "http://127.0.0.1:8080";

const state = {
  apiUrl: localStorage.getItem("meadow.apiUrl") || DEFAULT_API_URL,
  view: "chat",
  chatSessionId: null,
  configSection: "llm",
  sending: false,
  chatError: "",
  optimisticMessages: [],
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
    if (!state.optimisticMessages.length) {
      box.append(empty("直接输入日常问题、任务或要组合的工作流。"));
    }
  } else {
    for (const message of messages) box.append(messageBubble(message));
  }
  for (const message of state.optimisticMessages) box.append(messageBubble(message));
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
  state.optimisticMessages = [
    {
      message_id: `optimistic_${Date.now()}`,
      session_id: state.chatSessionId,
      role: "user",
      content,
      metadata: { optimistic: true },
    },
  ];
  state.sending = true;
  state.chatError = "";
  await render();
  let sent = false;
  try {
    await apiPost(`/chat/sessions/${encodeURIComponent(state.chatSessionId)}/messages`, {
      content,
      selected_skill_ids: skills.map((skill) => skill.skill_id),
      mode: "agent",
    });
    sent = true;
  } catch (error) {
    state.chatError = error.message || String(error);
  } finally {
    state.sending = false;
  }
  if (sent) state.optimisticMessages = [];
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
  const suffix = message.metadata?.optimistic ? " · 发送中" : "";
  meta.textContent = message.role === "user" ? `你${suffix}` : `Meadow${message.run_id ? ` · ${message.run_id}` : ""}`;
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
  const sectionIds = sections.map((item) => item.section);
  if (![...sectionIds, "raw"].includes(state.configSection)) state.configSection = "llm";
  const layout = div("config-layout");

  const listPanel = div("panel");
  const listHeader = div("panel-header");
  const listTitle = document.createElement("h2");
  listTitle.textContent = "配置分区";
  listHeader.append(listTitle);
  const listBody = div("panel-body session-list");
  const sectionLabels = {
    llm: ["大模型", "Provider / Model / Agent"],
    agents: ["Agent", "默认 Agent 与委派策略"],
    ui: ["界面", "语言、主题、密度"],
    mcp: ["MCP", "工具同步策略"],
    control: ["控制能力", "浏览器、桌面、移动"],
    raw: ["高级", "JSON 兜底编辑"],
  };
  const visibleSections = ["llm", "agents", "ui", "mcp", "control", "raw"].filter(
    (section) => section === "raw" || sectionIds.includes(section),
  );
  for (const section of visibleSections) {
    const [label, description] = sectionLabels[section] || [section, ""];
    const item = button("", async () => {
      state.configSection = section;
      await render();
    }, `session-item ${section === state.configSection ? "active" : ""}`);
    item.classList.toggle("active", section === state.configSection);
    const title = document.createElement("strong");
    title.textContent = label;
    const sub = document.createElement("span");
    sub.textContent = description;
    item.append(title, sub);
    listBody.append(item);
  }
  listPanel.append(listHeader, listBody);

  const editor = div("panel config-editor");
  const editorHeader = div("panel-header");
  const editorTitle = document.createElement("h2");
  editorTitle.textContent = sectionLabels[state.configSection]?.[0] || `配置：${state.configSection}`;
  editorHeader.append(editorTitle, pill("热更新"));
  const body = div("panel-body config-body");
  if (state.configSection === "llm") {
    const llm = (await apiGet("/config/llm")).config_section;
    body.append(renderModelConfig(llm.data || {}));
  } else if (state.configSection === "agents") {
    const llm = (await apiGet("/config/llm")).config_section;
    const agents = (await apiGet("/config/agents")).config_section;
    body.append(renderAgentConfig(agents.data || {}, normalizeLlmConfig(llm.data || {})));
  } else if (state.configSection === "ui") {
    const ui = (await apiGet("/config/ui")).config_section;
    body.append(renderSimpleConfigForm("ui", ui.data || {}, [
      { key: "locale", label: "语言", type: "select", options: ["zh-CN", "en-US"] },
      { key: "theme", label: "主题", type: "select", options: ["system", "light", "dark"] },
      { key: "density", label: "密度", type: "select", options: ["compact", "comfortable"] },
      { key: "default_view", label: "默认页面", type: "select", options: ["chat", "workspaces", "skills", "config"] },
    ]));
  } else if (state.configSection === "mcp") {
    const mcp = (await apiGet("/config/mcp")).config_section;
    body.append(renderSimpleConfigForm("mcp", mcp.data || {}, [
      { key: "auto_sync_to_agents", label: "自动同步到 Agent", type: "boolean" },
    ]));
  } else if (state.configSection === "control") {
    const control = (await apiGet("/config/control")).config_section;
    body.append(renderSimpleConfigForm("control", control.data || {}, [
      { key: "browser_enabled", label: "浏览器控制", type: "boolean" },
      { key: "desktop_enabled", label: "桌面控制", type: "boolean" },
      { key: "mobile_enabled", label: "移动控制", type: "boolean" },
    ]));
  } else {
    const rawSection = select(sectionIds);
    rawSection.value = sectionIds.includes("llm") ? "llm" : sectionIds[0];
    const rawEditor = textarea("JSON 配置", "");
    const loadRaw = async () => {
      const current = (await apiGet(`/config/${encodeURIComponent(rawSection.value)}`)).config_section;
      rawEditor.value = JSON.stringify(current.data, null, 2);
    };
    rawSection.addEventListener("change", loadRaw);
    await loadRaw();
    body.append(
      div("form-grid compact", field("分区", rawSection), field("JSON", rawEditor)),
      div("row-actions", button("保存 JSON", async () => {
        await apiPatch(`/config/${encodeURIComponent(rawSection.value)}`, { data: JSON.parse(rawEditor.value), merge: false });
        await render();
      }, "primary")),
    );
  }
  editor.append(editorHeader, body);
  layout.append(listPanel, editor);
  els.view.append(layout);
}

function renderModelConfig(data) {
  const config = normalizeLlmConfig(data);
  const root = div("config-stack");
  const summary = div("config-summary");
  summary.append(
    metric("Provider", config.providers.length),
    metric("模型", config.providers.reduce((total, provider) => total + provider.models.length, 0)),
    metric("Agent 绑定", config.agent_bindings.length),
  );
  root.append(summary);

  const global = div("settings-section");
  const activeProvider = select(config.providers.map((provider) => ({ value: provider.provider_id, label: provider.name || provider.provider_id })));
  activeProvider.value = config.active_provider_id || config.providers[0]?.provider_id || "";
  const activeModel = select(modelsForProvider(config, activeProvider.value).map((model) => ({ value: model.model_id, label: model.label || model.model_id })));
  activeModel.value = config.active_model_id || modelsForProvider(config, activeProvider.value)[0]?.model_id || "";
  activeProvider.addEventListener("change", () => {
    activeModel.innerHTML = "";
    for (const model of modelsForProvider(config, activeProvider.value)) {
      const option = document.createElement("option");
      option.value = model.model_id;
      option.textContent = model.label || model.model_id;
      activeModel.append(option);
    }
  });
  const showReasoning = checkboxInput(config.display?.show_reasoning !== false);
  const showBadges = checkboxInput(config.display?.show_model_badges !== false);
  global.append(
    div("section-title", "默认模型"),
    div("form-grid", field("默认 Provider", activeProvider), field("默认模型", activeModel), field("显示推理过程", showReasoning), field("显示模型能力", showBadges)),
    div("row-actions", button("保存默认模型", async () => {
      config.active_provider_id = activeProvider.value;
      config.active_model_id = activeModel.value;
      config.display = { ...(config.display || {}), show_reasoning: showReasoning.checked, show_model_badges: showBadges.checked };
      await saveLlmConfig(config);
    }, "primary")),
  );
  root.append(global);

  const providerSection = div("settings-section");
  providerSection.append(div("section-title", "模型接入"));
  for (const [index, provider] of config.providers.entries()) {
    providerSection.append(renderProviderEditor(config, provider, index));
  }
  providerSection.append(div("row-actions", button("新增 Provider", async () => {
    config.providers.push(newProviderConfig(`provider_${Date.now()}`));
    await saveLlmConfig(config);
  }, "primary")));
  root.append(providerSection);

  const bindingSection = div("settings-section");
  bindingSection.append(div("section-title", "Agent 模型绑定"));
  for (const [index, binding] of config.agent_bindings.entries()) {
    bindingSection.append(renderAgentBindingEditor(config, binding, index));
  }
  bindingSection.append(div("row-actions", button("新增 Agent 绑定", async () => {
    config.agent_bindings.push({
      agent_id: `agent_${config.agent_bindings.length + 1}`,
      label: "自定义 Agent",
      provider_id: config.active_provider_id || config.providers[0]?.provider_id || "",
      model_id: config.active_model_id || "",
      reasoning_model_id: "",
      image_model_id: "",
      show_reasoning: true,
    });
    await saveLlmConfig(config);
  })));
  root.append(bindingSection);
  return root;
}

function renderProviderEditor(config, provider, index) {
  const node = div("config-card provider-card");
  const header = div("config-card-header");
  header.append(
    div("", document.createElement("strong"), div("muted", provider.provider_id)),
    div("badge-row", badge(provider.kind), badge(provider.enabled !== false ? "启用" : "停用")),
  );
  header.querySelector("strong").textContent = provider.name || provider.provider_id;

  const providerId = input("provider_id", provider.provider_id || "");
  const name = input("名称", provider.name || "");
  const kind = select([
    { value: "openai-compatible", label: "OpenAI Compatible" },
    { value: "gemini", label: "Gemini" },
    { value: "anthropic", label: "Anthropic" },
    { value: "agent-cli", label: "Agent CLI / Cloud Code" },
    { value: "custom", label: "自定义" },
  ]);
  kind.value = provider.kind || "openai-compatible";
  const enabled = checkboxInput(provider.enabled !== false);
  const baseUrl = input("Base URL", provider.base_url || defaultBaseUrl(kind.value));
  const apiKeyEnv = input("API Key 环境变量", provider.api_key_env || "");
  const apiKey = input("API Key", provider.api_key === "***" ? "" : provider.api_key || "");
  apiKey.type = "password";
  apiKey.autocomplete = "off";
  const timeout = input("超时秒数", String(provider.timeout_seconds || 60));
  timeout.type = "number";
  timeout.min = "1";

  const modelList = div("model-list");
  for (const [modelIndex, model] of provider.models.entries()) {
    modelList.append(renderModelEditor(config, provider, model, modelIndex));
  }
  const newModelId = input("模型 ID", "");
  const newModelLabel = input("显示名称", "");

  node.append(
    header,
    div(
      "form-grid",
      field("Provider ID", providerId),
      field("名称", name),
      field("类型", kind),
      field("启用", enabled),
      field("Base URL", baseUrl),
      field("API Key Env", apiKeyEnv),
      field("API Key", apiKey),
      field("Timeout", timeout),
    ),
    div("section-subtitle", "模型与能力"),
    modelList,
    div("form-grid compact", field("模型 ID", newModelId), field("显示名称", newModelLabel)),
    div(
      "row-actions",
      button("保存 Provider", async () => {
        provider.provider_id = providerId.value.trim() || provider.provider_id;
        provider.name = name.value.trim() || provider.provider_id;
        provider.kind = kind.value;
        provider.enabled = enabled.checked;
        provider.base_url = baseUrl.value.trim() || defaultBaseUrl(kind.value);
        provider.api_key_env = apiKeyEnv.value.trim();
        if (apiKey.value.trim()) provider.api_key = apiKey.value.trim();
        provider.timeout_seconds = Number(timeout.value || 60);
        if (!config.active_provider_id) config.active_provider_id = provider.provider_id;
        await saveLlmConfig(config);
      }, "primary"),
      button("新增模型", async () => {
        const modelId = newModelId.value.trim();
        if (!modelId) return;
        provider.models.push(newModelConfig(modelId, newModelLabel.value.trim()));
        await saveLlmConfig(config);
      }),
      button("删除 Provider", async () => {
        config.providers.splice(index, 1);
        if (config.active_provider_id === provider.provider_id) config.active_provider_id = config.providers[0]?.provider_id || "";
        await saveLlmConfig(config);
      }, "danger"),
    ),
  );
  return node;
}

function renderModelEditor(config, provider, model, index) {
  const node = div("model-row");
  const modelId = input("model_id", model.model_id || "");
  const label = input("显示名称", model.label || "");
  const enabled = checkboxInput(model.enabled !== false);
  const contextWindow = input("上下文", String(model.context_window || ""));
  contextWindow.type = "number";
  const caps = normalizeCapabilities(model.capabilities || {});
  const capInputs = {};
  const capGrid = div("cap-grid");
  for (const cap of MODEL_CAPABILITIES) {
    const capInput = checkboxInput(Boolean(caps[cap.key]));
    capInputs[cap.key] = capInput;
    capGrid.append(field(cap.label, capInput));
  }
  node.append(
    div("form-grid compact", field("模型 ID", modelId), field("名称", label), field("启用", enabled), field("上下文窗口", contextWindow)),
    capGrid,
    div("row-actions",
      button("保存模型", async () => {
        model.model_id = modelId.value.trim() || model.model_id;
        model.label = label.value.trim();
        model.enabled = enabled.checked;
        model.context_window = Number(contextWindow.value || 0) || undefined;
        model.capabilities = Object.fromEntries(Object.entries(capInputs).map(([key, inputNode]) => [key, inputNode.checked]));
        await saveLlmConfig(config);
      }),
      button("删除模型", async () => {
        provider.models.splice(index, 1);
        await saveLlmConfig(config);
      }, "danger"),
    ),
  );
  return node;
}

function renderAgentBindingEditor(config, binding, index) {
  const node = div("config-card binding-card");
  const agentId = input("Agent ID", binding.agent_id || "");
  const label = input("名称", binding.label || "");
  const provider = select(config.providers.map((item) => ({ value: item.provider_id, label: item.name || item.provider_id })));
  provider.value = binding.provider_id || config.active_provider_id || "";
  const model = select(modelsForProvider(config, provider.value).map((item) => ({ value: item.model_id, label: item.label || item.model_id })));
  model.value = binding.model_id || "";
  const reasoningModel = input("推理模型 ID", binding.reasoning_model_id || "");
  const imageModel = input("图像模型 ID", binding.image_model_id || "");
  const showReasoning = checkboxInput(binding.show_reasoning !== false);
  provider.addEventListener("change", () => {
    model.innerHTML = "";
    for (const item of modelsForProvider(config, provider.value)) {
      const option = document.createElement("option");
      option.value = item.model_id;
      option.textContent = item.label || item.model_id;
      model.append(option);
    }
  });
  node.append(
    div("config-card-header", div("", strong(binding.label || binding.agent_id || "Agent 绑定"), div("muted", binding.agent_id || "")), badge("Agent")),
    div("form-grid", field("Agent ID", agentId), field("名称", label), field("Provider", provider), field("主模型", model), field("推理模型", reasoningModel), field("图像模型", imageModel), field("展示推理", showReasoning)),
    div("row-actions", button("保存绑定", async () => {
      binding.agent_id = agentId.value.trim() || binding.agent_id;
      binding.label = label.value.trim() || binding.agent_id;
      binding.provider_id = provider.value;
      binding.model_id = model.value;
      binding.reasoning_model_id = reasoningModel.value.trim();
      binding.image_model_id = imageModel.value.trim();
      binding.show_reasoning = showReasoning.checked;
      await saveLlmConfig(config);
    }, "primary"), button("删除绑定", async () => {
      config.agent_bindings.splice(index, 1);
      await saveLlmConfig(config);
    }, "danger")),
  );
  return node;
}

function renderAgentConfig(data, llmConfig) {
  const root = div("config-stack");
  const defaultAgent = input("默认 Agent", data.default_agent_id || "desktop_daily_agent");
  const defaultConnector = input("默认连接器", data.default_connector_id || "");
  const autoDelegate = checkboxInput(Boolean(data.auto_delegate));
  const fallbackProvider = select(llmConfig.providers.map((provider) => ({ value: provider.provider_id, label: provider.name || provider.provider_id })));
  fallbackProvider.value = data.model_policy?.fallback_provider_id || llmConfig.active_provider_id || "";
  const fallbackModel = select(modelsForProvider(llmConfig, fallbackProvider.value).map((model) => ({ value: model.model_id, label: model.label || model.model_id })));
  fallbackModel.value = data.model_policy?.fallback_model_id || llmConfig.active_model_id || "";
  const allowOverride = checkboxInput(data.model_policy?.allow_agent_override !== false);
  fallbackProvider.addEventListener("change", () => {
    fallbackModel.innerHTML = "";
    for (const model of modelsForProvider(llmConfig, fallbackProvider.value)) {
      const option = document.createElement("option");
      option.value = model.model_id;
      option.textContent = model.label || model.model_id;
      fallbackModel.append(option);
    }
  });
  root.append(
    div("settings-section",
      div("section-title", "Agent 策略"),
      div("form-grid", field("默认 Agent", defaultAgent), field("默认连接器", defaultConnector), field("自动委派", autoDelegate), field("允许 Agent 覆盖模型", allowOverride), field("兜底 Provider", fallbackProvider), field("兜底模型", fallbackModel)),
      div("row-actions", button("保存 Agent 配置", async () => {
        await apiPatch("/config/agents", {
          data: {
            ...data,
            default_agent_id: defaultAgent.value.trim() || "desktop_daily_agent",
            default_connector_id: defaultConnector.value.trim(),
            auto_delegate: autoDelegate.checked,
            model_policy: {
              ...(data.model_policy || {}),
              allow_agent_override: allowOverride.checked,
              fallback_provider_id: fallbackProvider.value,
              fallback_model_id: fallbackModel.value,
            },
          },
          merge: false,
        });
        await render();
      }, "primary")),
    ),
  );
  return root;
}

function renderSimpleConfigForm(section, data, fields) {
  const root = div("config-stack");
  const form = div("form-grid");
  const controls = {};
  for (const spec of fields) {
    let control;
    if (spec.type === "select") {
      control = select(spec.options);
      control.value = data[spec.key] || spec.options[0];
    } else if (spec.type === "boolean") {
      control = checkboxInput(Boolean(data[spec.key]));
    } else {
      control = input(spec.label, data[spec.key] || "");
    }
    controls[spec.key] = control;
    form.append(field(spec.label, control));
  }
  root.append(div("settings-section", form, div("row-actions", button("保存", async () => {
    const next = { ...data };
    for (const spec of fields) {
      const control = controls[spec.key];
      next[spec.key] = spec.type === "boolean" ? control.checked : control.value;
    }
    await apiPatch(`/config/${encodeURIComponent(section)}`, { data: next, merge: false });
    await render();
  }, "primary"))));
  return root;
}

const MODEL_CAPABILITIES = [
  { key: "text", label: "文本" },
  { key: "function_calling", label: "工具调用" },
  { key: "reasoning", label: "推理" },
  { key: "vision", label: "视觉理解" },
  { key: "image_input", label: "图片输入" },
  { key: "image_output", label: "图片输出" },
  { key: "audio_input", label: "音频输入" },
  { key: "audio_output", label: "音频输出" },
];

function normalizeLlmConfig(data) {
  const providers = Array.isArray(data.providers) ? data.providers : [];
  const normalizedProviders = providers.length ? providers : [newProviderConfig("openai_default")];
  for (const provider of normalizedProviders) {
    provider.provider_id = provider.provider_id || provider.id || provider.name || `provider_${Date.now()}`;
    provider.name = provider.name || provider.provider_id;
    provider.kind = provider.kind || provider.provider || provider.platform || "openai-compatible";
    provider.enabled = provider.enabled !== false;
    provider.base_url = provider.base_url || defaultBaseUrl(provider.kind);
    provider.api_key_env = provider.api_key_env || "";
    provider.api_key = provider.api_key || "";
    provider.timeout_seconds = provider.timeout_seconds || 60;
    provider.models = Array.isArray(provider.models) && provider.models.length ? provider.models : [newModelConfig(data.model || "gpt-4.1-mini", data.model || "GPT-4.1 mini")];
    for (const model of provider.models) {
      model.model_id = model.model_id || model.id || String(model.name || "");
      model.label = model.label || model.name || model.model_id;
      model.enabled = model.enabled !== false;
      model.capabilities = normalizeCapabilities(model.capabilities || {});
    }
  }
  return {
    ...data,
    active_provider_id: data.active_provider_id || normalizedProviders[0]?.provider_id || "",
    active_model_id: data.active_model_id || normalizedProviders[0]?.models[0]?.model_id || "",
    providers: normalizedProviders,
    agent_bindings: Array.isArray(data.agent_bindings) && data.agent_bindings.length ? data.agent_bindings : [{
      agent_id: "desktop_daily_agent",
      label: "日常 Agent",
      provider_id: data.active_provider_id || normalizedProviders[0]?.provider_id || "",
      model_id: data.active_model_id || normalizedProviders[0]?.models[0]?.model_id || "",
      reasoning_model_id: "",
      image_model_id: "",
      show_reasoning: true,
    }],
    display: data.display || { show_reasoning: true, show_model_badges: true },
  };
}

function normalizeCapabilities(caps) {
  const next = {};
  for (const cap of MODEL_CAPABILITIES) next[cap.key] = Boolean(caps[cap.key]);
  if (!Object.keys(caps).length) {
    next.text = true;
    next.function_calling = true;
  }
  return next;
}

function newProviderConfig(providerId) {
  return {
    provider_id: providerId,
    name: providerId === "openai_default" ? "OpenAI" : "新 Provider",
    kind: "openai-compatible",
    enabled: true,
    base_url: "https://api.openai.com/v1",
    api_key_env: "OPENAI_API_KEY",
    api_key: "",
    timeout_seconds: 60,
    models: [newModelConfig("gpt-4.1-mini", "GPT-4.1 mini")],
  };
}

function newModelConfig(modelId, label = "") {
  return {
    model_id: modelId,
    label: label || modelId,
    enabled: true,
    context_window: 0,
    capabilities: { text: true, function_calling: true, vision: false, reasoning: false, image_input: false, image_output: false, audio_input: false, audio_output: false },
  };
}

function defaultBaseUrl(kind) {
  if (kind === "gemini") return "https://generativelanguage.googleapis.com/v1beta";
  if (kind === "anthropic") return "https://api.anthropic.com/v1";
  return "https://api.openai.com/v1";
}

function modelsForProvider(config, providerId) {
  const provider = config.providers.find((item) => item.provider_id === providerId) || config.providers[0];
  return provider?.models || [];
}

async function saveLlmConfig(config) {
  await apiPatch("/config/llm", { data: config, merge: false });
  await render();
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

function field(label, control) {
  const node = div("field");
  const text = document.createElement("label");
  text.textContent = label;
  node.append(text, control);
  return node;
}

function checkboxInput(checked) {
  const node = document.createElement("input");
  node.type = "checkbox";
  node.checked = Boolean(checked);
  return node;
}

function metric(label, value) {
  const node = div("metric");
  const strongNode = document.createElement("strong");
  strongNode.textContent = String(value);
  const span = document.createElement("span");
  span.textContent = label;
  node.append(strongNode, span);
  return node;
}

function badge(text) {
  const node = div("badge");
  node.textContent = text || "unknown";
  return node;
}

function strong(text) {
  const node = document.createElement("strong");
  node.textContent = text;
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
  for (const item of values) {
    const value = typeof item === "object" ? item.value : item;
    const option = document.createElement("option");
    option.value = value;
    option.textContent = typeof item === "object" ? item.label : value;
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
