# Daily Agent Conversation Flow

更新时间: 2026-06-07 CST

## 目标

日常对话界面是 Meadow 的默认 Agent 入口。它需要对齐 GenericAgent 的核心体验: 用户可以自然对话, 也可以在同一个对话里让 Agent 搜索网页、控制浏览器/桌面/移动端、读写文件、执行代码、调用 MCP、运行 Workflow、委派子代理、并发分配任务、暂停/重试/清空上下文。

但 Meadow 不复制 GenericAgent 的运行时结构。GenericAgent 的精华是“模型在循环中自主选择工具并根据真实结果继续推理”; Meadow 需要把这个流程融入现有 Kernel 架构:

```text
Host / Desktop Chat
  -> Conversation & Task Hub
  -> Default Daily Agent Workflow
  -> Agent Runtime Loop
  -> Model Gateway
  -> CapabilityRuntime / Workflow Runtime / Agent Delegation / MCP / Workbench
  -> RuntimeEvent / ToolCallRecord / ArtifactRef / Memory
  -> ContextManager
  -> Agent Runtime Loop continues or finishes
  -> Desktop UI renders messages, tool calls, task status, approvals, artifacts, events
```

## 非目标

- 不在 UI 内判断意图。
- 不在 `DesktopChatService` 内硬编码天气、搜索、浏览器、任务拆分等关键词路由。
- 不让 Skill 直接绕过 Runtime/Policy 执行副作用。
- 不把外部 Agent 降级为一次性普通工具; 底层仍保留 AgentConnector 的 session、status、artifact、cancel、stream/progress 语义。

## 核心边界

### Desktop Chat

职责:

- 接收用户消息。
- 展示对话、任务、工具调用、审批、事件和 artifact。
- 发送 pause/retry/clear/resume 等用户控制。
- 通过应用层接口委托模型绑定解析和 Daily Agent 执行。

不负责:

- 选择工具。
- 直接调用模型。
- 直接执行浏览器、文件、代码、MCP、Workflow、子 Agent。
- 直接构造具体模型 Provider 或具体 Agent Runner。

当前代码边界:

- `ModelBindingProvider` 负责根据 Agent ID 解析 provider/model/API 配置, 默认实现为 `ConfigModelBindingProvider`, 测试或嵌入式宿主可使用静态 binding。
- `DailyAgentExecutor` 是日常 Agent 执行接口, 当前默认实现 `ContinuousDailyAgentExecutor` 适配现有连续工具循环。
- 后续 `Default Daily Agent Workflow` 落地时, 应新增 workflow-backed executor 替换默认实现, 而不是继续扩大 `DesktopChatService` 或让 UI 参与意图路由。

### Conversation & Task Hub

职责:

- 写入 Thread/Turn。
- 建立 Objective/Task。
- 创建或继续默认 Daily Agent Run。
- 维护 Thread、Task、Run、Message 的关联。
- 将用户控制转换为 Runtime control、human intervention、tool-call cancel/kill 等应用服务调用。

不负责:

- 执行节点。
- 直接调用模型或工具。
- 写长期记忆。

### Default Daily Agent Workflow

日常对话不是单次 chat completion, 而是一个内建 Agent Workflow:

```text
load_session
  -> retrieve_memory
  -> select_skills_and_capabilities
  -> build_context
  -> call_model
  -> route_action
  -> execute_capability_or_workflow_or_agent
  -> persist_turn_and_events
  -> update_working_memory
  -> continue_or_finish
```

这个 Workflow 必须可恢复、可审计、可暂停、可取消, 并把副作用交给 `CapabilityRuntime`、`RuntimeEngine`、`AgentDelegationBroker` 或 MCP/Workbench adapter。

### Agent Runtime

职责:

- 根据 Context/Memory/Skills/Capability catalog 调用模型。
- 解析模型行动意图。
- 将行动落成:
  - `ToolCallRequest`
  - `ExecutionCommand`
  - `PlanPatch`
  - `WorkflowAdapter` call
  - `AgentDelegation` request
  - `HumanIntervention` / user input request
- 接收真实结果并继续循环。

Agent Runtime 不能直接绕过策略执行副作用。

### Skill

Skill 是 Agent 可解释的程序性知识, 不是执行器。

Skill 负责告诉模型:

- 什么时候使用。
- 怎么使用。
- 推荐哪些工具、Workflow、Agent 类型或 MCP 能力。
- 有哪些约束和常见失败。

Skill 可以引用:

- atomic capabilities
- workflow ids
- mcp tool names
- agent connector roles
- procedural memory refs
- examples / artifacts

Skill 不能直接修改 workflow graph; 如需沉淀可复用流程, 应通过 autonomy/workflow induction 生成 draft `WorkflowSpec`。

Skill 可以在 `instructions` 中声明一行 `INDEX_HINT: ...`。ContextAssembler 只把这条用户可编辑的短提示放入 compact Skill index, 用于首轮行动决策；完整 SOP 仍需通过 `skill_open` 渐进式读取。框架不能按具体网站、具体任务或具体 Skill ID 写特殊执行流程。

### Browser Atomic Capability 与 SOP

GenericAgent 的可取之处不是把“浏览器搜索”写成固定业务流, 而是把浏览器拆成少量稳定原子能力, 再用 SOP 约束模型如何组合。Meadow 对齐这个边界:

- `browser_scan`: 读取浏览器 targets 和当前页面观察结果, 包括 `page.title`、`page.url`、`page.text`、`page.links`、`page.search_results`、`page.visible_cards`。
- `browser_navigate`: 打开 URL 或在真实浏览器标签页导航。
- `browser_execute_js`: 精确执行 JS, 用于点击、滚动、DOM 提取、动态页面处理和扩展桥能力。
- `builtin.atomic.web_research`: SOP/Skill, 说明何时扫描标签页、何时导航/搜索、如何从搜索结果页继续打开候选来源、如何核验 title/url/facts, 以及失败时如何切换 HTTP 或下一个候选链接。

动态信息流页面（推荐、最新帖子、Feed、社交媒体卡片流）不应依赖反复全页 `browser_scan`。模型应在绑定的 `target_id` 上用 `browser_execute_js` 完成 refresh、scroll、click 和 DOM 结构化抽取, 返回紧凑 JSON, 例如 `title/text/url/author/time/metrics`。如果一次抽取为空, 先等待或滚动再抽取；仍为空再报告登录、反爬或页面结构不可读。

应用层 runner 只能负责模型调用、工具调用、结果回灌、事件记录和预算控制; 不能内置“搜索必须打开第几个页面”“小红书用某个固定脚本”等任务策略。任务策略必须来自 Skill/SOP、记忆、上下文或后续编译出的 Workflow。

### Browser Target Ownership / Lease / Scope

日常对话和多 Agent 浏览器任务必须同时满足两个目标:

- 全局感知: 日常 Agent 可以通过 `browser_scan(tabs_only=true)` 或普通 `browser_scan` 看到所有真实浏览器标签页, 用于判断当前环境、选择目标页、向用户说明可操作对象。
- 受控操作: 一旦要导航、执行 JS、点击、滚动、持续读取某个页面, 必须绑定到明确的 `target_id` 或当前 run/agent/scope 已拥有的 active target, 不能依赖“当前激活标签页”。

为此 `ControlWorkbench` 增加浏览器目标协调层:

- `ControlOwnerContext`: 由 `CapabilityRuntime` 从 `run_id`、`agent_id`、`task_id`、`scope`、`workbench_id` 生成, 表示一次控制动作的逻辑所有者。
- `BrowserTargetOwnership`: 记录 `target_id` 属于哪个 run/agent/scope, 以及最近用途和动作。
- `BrowserTargetLease`: 执行控制动作时的短租约。读取使用 read lease, 导航/JS 使用 mutation lease, 关闭/重载等未来动作使用 exclusive lease。
- `BrowserTargetCoordinator`: 不依赖 BrowserLink/Playwright 具体实现, 只负责目标解析、归属校验、租约冲突和结果注解。

默认规则:

- `browser_scan(tabs_only=true)` 只列出 tabs, 不抢占目标。
- 无 `target_id` 的 `browser_scan` 可以读取当前后端选择的页面, 但只有当该目标未被其他 scope 拥有时才绑定到当前 scope。
- `browser_navigate` 无 `target_id` 时创建新标签页并绑定当前 scope; 有 `target_id` 时绑定/导航该目标。
- `browser_execute_js` 必须有明确 `target_id` 或当前 scope 已有 active target; 如果目标属于其他 scope, 返回 `browser_target_owned_by_other_scope`。
- 子 Agent 并发浏览器任务应优先创建或领取自己的 target/lane, 后续工具调用必须携带或继承该 target, 避免跨 agent 误读、误点、误关闭。

UI 层应该展示 target 的 `ownership`、`owned_by_current_scope`、`active_lease` 和 `browser_scope.active_target_id`, 让用户能看到哪个 Agent 正在使用哪个标签页, 并能在后续实现里进行释放、转交、强制接管或关闭。

### Capability / Workflow / Agent / MCP Catalog

模型需要通过 Context 看到一份经过裁剪的能力目录:

- Atomic capabilities: 文件、HTTP、浏览器、桌面、移动、代码执行、记忆埋点、用户输入。
- Workflow catalog: 已注册 workflow/template 及其适用条件、输入输出。
- Agent delegation catalog: 可用 connector、agent type、委派/状态/取消语义。
- MCP catalog: 已启用 MCP server 和 tool 描述。
- Workbench/control catalog: browser/desktop/mobile target 和可用动作。
- Policy context: 当前 grant、审批要求、风险边界。

能力目录必须由 ContextManager/ToolVisibility 策略裁剪, 不默认把所有 schema 全塞给模型。

## 标准执行流

### 普通对话

```text
User Turn
  -> Hub writes Thread/Turn
  -> Daily Agent Run
  -> Context includes recent conversation + active skills + relevant memory
  -> Model returns final response
  -> Assistant Turn persisted
```

### 网页/浏览器任务

```text
User: 搜索今天的天气
  -> Context includes Web/Browse Skill + http/browser capabilities
  -> Model selects Skill and emits tool call
  -> CapabilityRuntime policy check
  -> HTTP/browser adapter executes
  -> ToolResult + ToolCallRecord + Audit/Event
  -> Context receives tool result summary/artifact ref
  -> Model answers from real result
```

### 工作流任务

```text
User: 按代码评审流程检查这个仓库
  -> Context includes Workflow catalog and matching Skill
  -> Model selects workflow_run / ExecutionCommand
  -> RuntimeEngine creates subworkflow run
  -> Parent agent awaits/observes result
  -> Artifacts and decision returned to conversation
```

### 子代理与并发任务

```text
User: 拆成前端/后端/测试三个子任务并发执行
  -> Context includes Agent delegation catalog and TaskBoard/AgentPool state
  -> Model creates task split plan
  -> CollaborationWorkbenchService creates a parallel-delegation workbench
  -> AgentDelegationBroker starts child tasks or TaskBoard claims
  -> Child agents run independently with lineage, status, cancel, artifact handoff
  -> Parent agent polls/waits and summarizes structured outputs
```

### 群聊 / 多 CLI / 技术评审工作台

```text
User: 组织几个子 Agent 做技术评审
  -> Context includes Workbench Skill + Collaboration Workbench API schema
  -> Model selects workbench kind: group_chat / cli_collaboration / technical_review / parallel_delegation
  -> CollaborationWorkbenchService creates members, channel, task slices, taskboard items
  -> Optional auto_start delegates slices to configured CLI/remote agent connectors
  -> UI renders channel timeline, task slices, delegation cards, approvals, artifacts
  -> Model or user can send messages, create decision artifacts, cancel/retry slices
  -> If missing inputs/authorization, model calls user_input_request and the chat turn waits for the user
```

详见 `docs/collaboration-workbench-architecture.md`。工作台是日常对话扩展到多 Agent 协作空间的应用层入口，不改变 Runtime/Capability/Policy 的职责边界。

日常 Agent 可见的工作台工具包括 `workbench_create`、`workbench_status`、`workbench_message`、`workbench_decision`、`workbench_cancel`。模型可以在多轮工具循环里先创建工作台，再根据真实 `workbench_id` 继续发送主持人消息或查询状态。

### 用户输入/审批/暂停

```text
Model needs missing info
  -> user_input_request / HumanIntervention
  -> Run enters waiting_input/interrupted state
  -> Desktop UI shows prompt
  -> User reply resumes run
```

高风险副作用:

```text
Tool call requires approval
  -> PolicyEngine returns require_approval
  -> ApprovalRequest persisted
  -> UI shows structured preview
  -> approve/reject resumes or fails current step
```

## 实现阶段

### P0: 固化流转和约束

- 记录本文档。
- 更新 TODO, 明确桌面日常对话必须走 Conversation & Task Hub + Default Daily Agent Workflow。
- 移除或收敛 chat service 内的旁路 agent loop。

### P1: Hub MVP

- 新增 `ConversationTaskHub` 应用服务。
- 将 desktop chat session/message 映射到 Thread/Turn 概念。
- 用户消息创建 Task/Run 关联。
- pause/retry/clear 映射到 run/task/message 控制。

### P2: Daily Agent Loop MVP

- 在 Agent Runtime 增强多轮 tool loop。
- 模型输出归一化为 action:
  - final response
  - capability call
  - workflow call
  - agent delegation
  - user input request
- 工具结果回灌 Context, 直到 finish 或达到上限。

### P3: Capability Catalog

- 内置原子能力以 Skill + capability schema 进入上下文。
- Workflow catalog、Agent delegation catalog、MCP catalog、Workbench catalog 进入上下文。
- 接入 tool visibility pruning 和 policy context。

### P4: Desktop UI 对齐

- 日常对话展示 run/task 状态。
- 展示 tool calls、审批、artifact、子代理 delegation card、workflow progress。
- pause/retry/clear/resume 操作对应真实 runtime 控制。

## 验收标准

- 在日常对话中, “搜索今天的天气”会由模型选择 Web/Browse Skill 并执行真实 HTTP 或 browser capability, 不是文本说明。
- 在日常对话中, “拆给多个子代理并发执行”会创建可追踪的 child delegation/task, 并能查看状态和取消。
- 在日常对话中, “运行某个 workflow”会创建 Runtime run, 并能通过事件流观察。
- 所有副作用能力都有 ToolCallRecord、PolicyDecision、Audit/Event。
- UI 只消费 API/事件, 不包含模型意图判断逻辑。
