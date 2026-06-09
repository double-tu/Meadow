# Agent Execution Recovery Architecture

更新时间: 2026-06-08 CST

## 目标

Meadow 的日常 Agent、工作台 Agent、CLI Agent 和子 Agent 都会遇到同一类问题: 模型在工具循环中没有推进、重复读取同一状态、工具失败后没有换策略、达到最大轮次后只返回空结果或无效摘要。这个问题不能靠给某个网站、某个任务、某个 Skill 写特殊分支解决, 而要成为 Agent Runtime 的通用执行协议。

本设计的目标是吸收 GenericAgent 与 Claude Code 类工具里有效的机制, 并融入 Meadow 现有 `ContextAssembler`、`Skill/SOP`、`CapabilityRuntime`、`RuntimeEvent`、`Artifact` 和桌面可视化架构:

- 工具结果不仅是数据, 也是下一轮模型推理的协议输入。
- 无进展、重复失败、空回复、截断、最大轮次等异常必须模型可见, 让模型先修正策略。
- 如果仍失败, 最终输出必须包含可行动诊断: 原始目标、已尝试动作、有效证据、失败原因、下一步建议。
- 运行时只做通用监测、状态转换、预算和治理, 不硬编码具体业务流程。
- Skill/SOP 负责告诉模型如何组合原子能力, 用户可编辑和新增。

## 参考机制抽象

### GenericAgent 可吸收点

- 工具执行结果带 `data`、`next_prompt`、`should_exit`: 工具可以向下一轮模型注入“下一步应关注什么”, 而不是只返回裸数据。
- `do_no_tool` 修复流: 空回复、流中断、输出截断、大段代码但未调用工具等情况不会直接完成, 而是回灌修复提示。
- `turn_end_callback`: 每轮强制更新短摘要/working memory, 防止长任务丢失目标。
- 周期性 `[DANGER]` 提醒: 轮次增长或重复失败时要求模型换策略、探测真实状态或请求用户。
- Tool/SOP 描述中包含操作规范: 例如浏览器任务应少量 scan、更多精确 JS/打开候选来源页、失败后做 checkpoint。

### Claude Code 类机制可吸收点

- Query loop 是状态机, 每轮有明确 transition reason, 例如 `next_turn`、`max_output_tokens_recovery`、`stop_hook_blocking`、`reactive_compact_retry`。
- Stop hook 可以阻止一次看似完成的回答, 将错误/缺口作为新的用户侧消息回灌模型修正。
- Tool error 是模型可见的 `tool_result is_error=true`, 包括未知工具、参数校验、权限拒绝、运行失败。
- 最大轮次不是静默失败, 而是产生结构化 terminal reason / attachment, 让 UI 和用户知道为什么停下。
- 工具编排区分并发安全能力与上下文修改能力, 运行时维护执行上下文而不是让工具互相污染。

Meadow 不照搬这些项目的代码或文件结构, 只吸收机制: 模型可见协议、状态转换、hook、诊断合成和能力目录/SOP 披露。

## 非目标

- 不在 runner 中硬编码“小红书怎么刷”“天气怎么搜”“百度第几个结果怎么点”等特殊流程。
- 不用规则替代模型的任务规划。规则只负责发现无进展、保护预算、提供修复提示和终止诊断。
- 不让 Skill 直接执行副作用。所有工具调用继续经过 `CapabilityRuntime`、Policy、Audit 和 Event。
- 不把大页面、日志、子 Agent 输出塞进事件或 prompt。大内容必须 artifact 化并按需读取。

## 执行状态机

`ContinuousAgentRunner` 后续应显式维护每轮 transition:

```text
start
  -> model_turn
  -> tool_execution
  -> tool_result_feedback
  -> progress_hook_review
  -> next_turn | repair_turn | stop_hook_blocking | awaiting_approval | waiting_for_user
  -> final_answer | max_turns_terminal | failed_terminal
```

建议的 transition reason:

- `start`: 首轮构建上下文。
- `next_turn`: 工具返回了新证据, 继续推理。
- `tool_error_recovery`: 工具失败但可恢复, 回灌错误和修复建议。
- `no_tool_recovery`: 模型没有给出可展示内容且未调用工具, 回灌协议修复。
- `no_progress_recovery`: hook 发现重复观察、重复失败或没有新证据, 要求换策略。
- `stop_hook_blocking`: 模型试图完成, 但证据不足或输出无效, 阻止完成并要求修正。
- `max_output_tokens_recovery`: provider/adapter 判断输出被截断, 要求压缩或继续。
- `context_compact_retry`: 上下文超预算或大结果被 artifact 化后重新注入短状态。
- `awaiting_approval`: capability policy 需要审批。
- `waiting_for_user`: 缺少必要输入或用户确认。
- `max_turns_terminal`: 预算耗尽, 输出结构化诊断。
- `failed_terminal`: 连续修复失败或不可恢复错误。
- `completed`: 有证据支持的最终回答。

这些 reason 应进入模型可见工具回灌、RuntimeEvent payload 和 UI activity projection。

## 核心接口设计

### ExecutionTransition

```python
@dataclass(slots=True)
class ExecutionTransition:
  run_id: str
  turn: int
  reason: str
  original_goal: str
  metadata: dict[str, Any] = field(default_factory=dict)
```

作用:

- 记录当前轮为什么继续、修复、阻塞或结束。
- 给 hook 和诊断合成器提供统一输入。
- 作为 UI process visibility 的稳定字段。

### ToolOutcomeView

运行时 hook 不应依赖具体 adapter 类型, 只看统一视图:

```python
class ToolOutcomeView(Protocol):
  name: str
  capability_id: str
  input: dict[str, Any]
  ok: bool
  output: dict[str, Any]
  error: dict[str, Any] | None
  requires_approval: bool
```

当前 `ContinuousToolCallRecord` 可以直接满足这个协议。

### ProgressHookResult

```python
@dataclass(slots=True)
class ProgressHookResult:
  reason: str
  severity: Literal["info", "warning", "blocking", "terminal"]
  message: str
  repair_hint: str
  metadata: dict[str, Any] = field(default_factory=dict)
  model_visible: bool = True
```

含义:

- `warning`: 写入事件/UI, 可选择回灌模型。
- `blocking`: 必须作为模型可见修复消息回灌, 本轮不能静默继续同样动作。
- `terminal`: 运行时可以停止, 但必须通过诊断合成器输出用户可读结果。

### AgentProgressHook

```python
class AgentProgressHook(Protocol):
  def after_tool_results(
    self,
    *,
    transition: ExecutionTransition,
    turn_records: list[ToolOutcomeView],
    all_records: list[ToolOutcomeView],
    action_history: list[str],
  ) -> list[ProgressHookResult]:
    ...

  def before_final_answer(
    self,
    *,
    transition: ExecutionTransition,
    model_output: dict[str, Any],
    all_records: list[ToolOutcomeView],
  ) -> list[ProgressHookResult]:
    ...
```

实现要求:

- Hook 只基于通用信号: 工具名称、输入签名、输出摘要、错误类型、证据增量、轮次和预算。
- Hook 可以提示打开 Skill/SOP、切换工具、读取 artifact/event、请求用户输入。
- Hook 不知道业务站点和任务关键词。

## NoProgressHook 设计

`NoProgressHook` 是第一批核心 hook, 用于识别“看起来在行动但没有推进”的循环。

### 输入信号

- 重复工具输入签名: 同一 `capability_id + stable_input` 在非连续轮次反复出现。
- 低信息浏览器观察: 多次 `tabs_only`、只返回 tabs 无页面正文、同 URL/标题/文本指纹重复。
- 重复 Skill 打开: 多次打开同一 Skill, 但之后没有执行推荐能力或没有获得新证据。
- 重复失败: 相同 capability、相近输入、相同 error type 连续或交替出现。
- 无新增证据: 本轮输出没有新增 URL、标题、正文片段、artifact ref、文件 diff、命令结果、子 Agent 状态变化。
- 无效完成: 模型最终回答没有引用工具证据, 或只说“已完成/没有内容”。

### 判定策略

建议默认阈值保守:

- 第 2 次低信息重复: `warning`, 写入 action history。
- 第 3 次低信息重复: `blocking`, 回灌模型要求换策略。
- blocking 后仍重复 2 次: `terminal` 或由 runner 在最大轮次时纳入失败诊断。

低信息判定要可替换, 初期可内置以下通用 summarizer:

- `browser_scan(tabs_only=true)` 没有 page/facts/cards/search_results。
- `browser_scan` 返回相同 `page.url + page.title + text_prefix_hash`。
- `browser_execute_js` 返回空数组、空字符串、null, 且 input 与 target 没变化。
- `http_request` 返回相同 URL 的反爬/登录/错误页。
- `agent_parallel_delegate` 返回 adapter 未配置或全部子任务未启动。

这些不是业务流程, 而是通用进展信号。

### 模型可见修复消息

Hook 产生的修复消息进入下一轮 `tool_results` content:

```json
{
  "type": "execution_hook",
  "hook": "no_progress",
  "severity": "blocking",
  "observed": [
    "browser_scan tabs_only returned the same target list 3 times",
    "skill_open builtin.atomic.web_research repeated without new evidence"
  ],
  "instruction": "Do not repeat the same action. Open the relevant SOP if needed, switch to a different capability/input/source, inspect a specific target/page, or ask the user for missing information. If the task cannot proceed, produce a concrete blocker report with evidence."
}
```

这样模型知道自己为什么被纠偏, 也知道可以选择哪类下一步, 但具体任务流程仍由模型和 Skill/SOP 决定。

## Research Ledger 设计

GenericAgent 的深度搜索能力有一个关键机制: 模型持续看到“已经查过什么、发现了哪些候选、哪些证据尚未核验、哪些动作失败”。Meadow 不采用项目私有的 history 字符串拼接, 而是在 `ContinuousAgentRunner` 内新增被动的 `ResearchLedger`:

- **只提炼, 不决策**: 账本从工具结果里提取 visited sources、evidence、candidate sources、failures、strategy notes, 不决定下一步工具或具体网站。
- **候选与证据分离**: 搜索结果页、链接列表、论坛列表等进入 `candidate_sources`; 打开的页面正文、动态卡片、JS 抽取内容才进入 `evidence`。
- **模型可见**: 每轮构造上下文时注入 `research_ledger` system message; 工具结果回灌时也带同一账本摘要, 模型可据此继续纵向/横向探索。
- **通用触发**: 用户目标包含搜索、调研、浏览器、最新、当前等意图, 或工具调用涉及 browser/http/search 时启用。账本不绑定站点、品牌、任务特例。
- **策略压力**: 当发现搜索结果候选但尚未打开来源页时, 写入 `search_results_need_source_open`; 当动态页面已有卡片但需要更多详情时, 写入 `dynamic_page_structured_cards`。这些是通用提示, 不是流程硬编排。

账本与 `RunAnchor` 的区别:

- `RunAnchor` 保留任务连续性、对话历史和动作摘要。
- `ResearchLedger` 保留探索性任务的证据状态和候选队列。
- `NoProgressHook` 负责发现无进展并提示切策略。
- Skill/SOP 仍负责具体程序性知识, 如浏览器调研如何打开来源、滚动、抽取 DOM、交叉核验。

这形成 GenericAgent 风格但更模块化的执行闭环:

`ToolResult -> ResearchLedger/RunAnchor/NoProgressHook -> ContextAssembler -> Model decides next tool`.

## Stop Hook 设计

`StopHook` 处理“模型想结束, 但输出不满足任务”的情况。它不评判事实真伪, 只检查结构性缺口:

- 用户要求使用浏览器/工具, 但没有任何相关工具证据。
- 用户要求调研/搜索/打开页面, 但最终输出没有来源、页面标题、URL、页面观察或明确失败原因。
- 任务达到最大轮次或工具失败, 但回答没有说明失败原因。
- 输出为空、只有占位文本、只有“已完成但无内容”。
- 工具已有有效证据, 但模型没有把证据转成用户可读结论。

StopHook 返回 `blocking` 时, runner 追加模型可见消息, 关闭或裁剪工具面可选, 要求模型基于已有证据给最终答案或给失败诊断。

## 最大轮次与失败诊断合成器

任何 terminal 都不能只返回 “达到最大执行轮次” 或空输出。诊断合成器必须输出统一结构:

```json
{
  "summary": "未完成原始任务: ...",
  "status": "max_turns_exceeded",
  "original_goal": "...",
  "turns": 16,
  "tool_count": 24,
  "completed_actions": ["..."],
  "evidence": [
    {"type": "browser_page", "title": "...", "url": "...", "observation": "..."}
  ],
  "failures": [
    {"capability_id": "atom.control.browser.scan", "error": "repeated_tool_call_guard"}
  ],
  "no_progress_causes": [
    "多次重复读取同一标签页列表, 没有打开候选来源页",
    "同一 Skill 重复打开后未产生新的工具证据"
  ],
  "next_steps": [
    "继续时应先绑定/打开目标标签页并读取页面正文",
    "如果浏览器后端不可用, 切换 HTTP 请求或请求用户授权/登录"
  ]
}
```

UI 可以展示 `summary` 作为聊天消息, 把 `evidence/failures/no_progress_causes` 折叠到过程面板。

## 与七层上下文/Skill/SOP 的关系

- Layer 1 System/Policy: 注入简短失败升级原则和“无进展必须换策略”的元规则。
- Layer 3 Skill/Tool Index: SkillCard 提供能力发现; full SOP 通过 `skill_open` 渐进披露。
- Layer 4 Working Memory: 保存当前目标、已尝试策略、失败计数、active target、workbench/delegation 状态。
- Layer 6 Event/Artifact: 大工具输出和诊断进入 artifact/event, prompt 只带摘要/ref。
- Layer 7 Procedural Memory: 成功修复路径后续可沉淀为 SOP/Skill evolution candidate。

Runner 的 progress hook 只能生成“你无进展/缺证据/应换策略”的机制性信号; “浏览器调研应该打开候选来源页”“并行搜索如何拆分”这类程序性知识必须来自 Skill/SOP 或 procedural memory。

## UI 与过程可视化

桌面端应把 hook/transition 作为过程数据展示, 而不是只显示“等待 Agent 返回”:

- 主聊天区: 用户消息立即显示; Agent 最终回答或失败诊断显示在消息流。
- 右侧/折叠面板: 当前 run 的 turn、transition、tool calls、hook warnings、子 Agent、审批、artifact。
- 子 Agent/工具调用: 可折叠, 支持查看输入摘要、输出摘要、错误、重试/停止/删除动作。
- 最大轮次或失败: 展示诊断卡, 包含“为什么停下”“已有证据”“建议继续动作”。

UI 不参与判断任务流程, 只消费 `RuntimeEvent`、`ProcessVisibilityProjector`、`ToolCallRecord` 和诊断输出。

## 实现计划

### P0: 文档与契约

- 新增本文档。
- 更新 `TODO.md`, 将 execution recovery 作为当前阶段目标之一。
- 明确 GenericAgent/Claude Code 的机制映射和 Meadow 的非目标。

### P1: Runtime 协议

- 新增 `agent_kernel/agents/execution_hooks.py`。
- 定义 `ExecutionTransition`、`ProgressHookResult`、`AgentProgressHook`、`NoProgressHook`、`StopHook`、`ExecutionDiagnosticSynthesizer`。
- 保持 hook 不依赖具体 runner/UI/adapter。

### P2: Runner 接入

- `ContinuousAgentRunner` 维护 transition reason。
- 工具结果回灌中加入 hook result。
- 无工具/空回复/截断/重复工具调用统一走 model-visible repair turn。
- 最大轮次调用诊断合成器, 不再只用启发式 fallback。
- 保留当前 feed/browser 兜底摘要能力, 但将其作为证据抽取的一部分。

### P3: 测试与验收

- 测试重复 `browser_scan(tabs_only=true)` 与 `skill_open` 交替时触发 `no_progress_recovery`。
- 测试 hook 修复消息会出现在下一轮模型输入中。
- 测试达到最大轮次时输出包含 original goal、turn/tool count、evidence、failures、no_progress_causes、next_steps。
- 测试 stop hook 阻止“已完成但无内容”的最终回答。
- 测试已有有效页面/Feed 证据时仍能给出用户可读摘要。

### P4: UI/事件投影

- RuntimeEvent 增加或复用 `AGENT_TURN_COMPLETED` payload 中的 `transition`、`hook_results`。
- `ProcessVisibilityProjector` 投影 hook warning/blocking/terminal activity。
- 桌面 run 过程面板折叠展示 hook 和诊断。

## 验收标准

- 同一任务失败时, 用户一定能看到可行动原因, 不出现空白或“已完成但无内容”。
- 模型重复相同低信息工具调用时, runner 会把 no-progress 诊断回灌模型, 并要求换策略。
- 达到最大轮次时, 输出能解释已做什么、证据是什么、为什么停下、下一步怎么继续。
- 浏览器、HTTP、子 Agent、MCP、工作流失败都走同一套协议, 不为具体网站/任务写特殊分支。
- Skill/SOP 仍是任务策略来源; runtime hook 只负责机制性纠偏。
