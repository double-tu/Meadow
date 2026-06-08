# Core Agent Context Architecture

更新时间: 2026-06-08 CST

## 目标

Meadow 的日常 Agent、工作台 Agent、CLI Agent 和未来多端入口都需要一份稳定、短小、可演化的核心上下文。它的作用不是替代七层上下文机制, 而是在七层机制中补上 GenericAgent 最有效的部分:

- 模型一进入任务就知道自己具备哪些能力、能力在哪里、什么时候打开 Skill/SOP。
- 模型知道失败后如何升级, 什么时候探测环境, 什么时候请求用户, 什么时候记录 working memory。
- 模型知道记忆不是随便写的文本, 必须来自工具验证后的事实。
- Skill/SOP 能按统一模板生成、索引、打开、验证和演化, 而不是散落在 prompt 字符串里。

GenericAgent 的可取结构是 `L0 元规则 + L1 极简索引 + L2 稳定事实 + L3 SOP/脚本 + L4 会话归档`。Meadow 不照搬文件布局, 而是把它映射到现有 `ContextAssembler`、`SkillService`、`MemoryFacade`、`CapabilityRuntime` 和七层上下文。

## 核心原则

1. **存在性优先**: 默认上下文只告诉模型“有什么能力存在、从哪里打开”, 不把长 SOP 全塞进去。
2. **SOP 是程序性知识, 不是执行器**: SOP 指导模型如何组合工具; 所有副作用仍通过 `CapabilityRuntime`、Policy、Audit、Event。
3. **No Execution, No Memory**: 长期记忆、事实和程序性经验必须来自成功工具调用、运行结果、事件或 artifact, 不能来自模型猜测。
4. **探测优先**: 失败时先读取错误、状态、日志、页面、事件或上下文, 再重试或换方案。
5. **失败升级**: 第 1 次失败理解错误; 第 2 次失败探测环境边界; 第 3 次失败换策略或请求用户。禁止无新信息的重复调用。
6. **渐进披露**: `CoreContext` -> `SkillCard` -> `SkillSpec` -> `SkillResource` -> artifact/event/memory, 逐层按需打开。
7. **短索引, 长资源**: 能用 skill/resource 名字自解释的, 索引不加说明; 只有反直觉触发词才放进核心索引。

## 七层映射

### Layer 1: System / Policy

新增 `CoreAgentContextProvider`, 默认注入短宪法:

- 你是 Meadow Agent, 通过工具和 Skill/SOP 完成真实任务。
- 需要实时信息、浏览器、文件、代码、MCP、Workflow、子 Agent、工作台时, 优先调用工具, 不要只说明自己可以做。
- 工具结果会回灌; 基于真实结果继续推理。
- 副作用遵循审批和权限; 不可逆或高风险操作请求用户或等待 approval。
- 失败遵循探测和升级规则。

### Layer 2: Agent Profile

保留现有 agent profile, 后续扩展:

- host surface: desktop/chat/workbench/cli/api
- model capabilities: vision/audio/image/reasoning/tool-use/context limit
- enabled catalogs: browser/workflow/mcp/workbench/delegation

### Layer 3: Skill / Tool Index

增强现有 `SkillToolIndexLayerProvider`:

- 注入 `capability_navigation_index`: 场景触发词 -> skill_id/tool/resource。
- 默认只包含 SkillCard 字段和短索引。
- 明确提示: 复杂任务、浏览器任务、多 Agent/工作流任务、技术评审、记忆更新前, 先 `skill_open` 或 `context_expand` 打开相关 SOP。

### Layer 4: Working Memory

Working memory 应承载:

- 当前目标、用户约束、关键参数。
- active run/task/workbench/delegation IDs。
- 已读 SOP 与当前流程阶段。
- 失败次数、失败原因、已尝试策略。
- 下一步计划。

模型应在以下场景调用 `memory_checkpoint`:

- 任务开始且不是 1-2 步简单任务。
- 打开并决定使用 SOP 后。
- 子任务切换或上下文可能被压缩前。
- 同一能力连续失败后。
- 多 Agent/工作台/浏览器任务进入新阶段时。

### Layer 5-7

保持现有会话窗口、episodic/artifact、semantic/procedural memory 机制。核心上下文只给出读取规则:

- 大内容不进事件和 prompt, 保存 artifact ref。
- 需要细节时使用 `artifact_read`、`event_search`、`memory_search`、`memory_read`。
- 长任务完成后由 curator/settlement 产生 episodic/semantic/procedural memory。

## 核心能力导航索引

默认索引应短小, 类似:

```text
[Meadow Capability Navigation]
实时/网页/天气/新闻/打开网页: builtin.atomic.web_research -> skill_open, browser_scan, browser_navigate, browser_execute_js, http_request
当前浏览器/页面/标签页/小红书/动态页面: builtin.atomic.desktop_mobile_control + builtin.atomic.web_research
复杂多步骤/需要计划/验证: builtin.sop.planning
多Agent/群聊/CLI协作/并行搜索/技术评审工作台: builtin.atomic.collaboration_workbench
子Agent委派/状态/取消: builtin.atomic.agent_delegation
代码/测试/命令验证: builtin.atomic.code_execution
文件/项目修改: builtin.atomic.workspace
缺少信息/需要授权: builtin.atomic.user_input
记忆/经验沉淀: builtin.sop.memory_governance + memory_checkpoint + memory_evolution_note
```

这不是硬路由。它只帮助模型发现“能力存在”, 具体是否使用和如何使用仍由模型基于用户目标、Skill/SOP、工具 schema 和上下文决定。

## Skill/SOP 模板

Meadow Skill 应分三层。

### SkillCard

始终可索引, 适合默认上下文:

```yaml
skill_id: builtin.sop.browser_research
name: 浏览器网页研究 SOP
description: 通过真实浏览器或 HTTP 获取、打开、读取、核验网页信息。
when_to_use: 用户要求搜索、打开网页、查看当前浏览器、获取最新信息、操作动态网页时。
trigger_keywords:
  - 搜索
  - 今天/当前/最近
  - 浏览器
  - 打开网页
recommended_tools:
  - skill_open
  - browser_scan
  - browser_navigate
  - browser_execute_js
  - http_request
risk_level: external_read_or_mutation
```

### SkillSpec

通过 `skill_open` 按需加载:

```markdown
# Skill: <name>

## Purpose
一句话说明它解决什么任务。

## When To Use
触发场景和反触发场景。

## Inputs / Preconditions
需要用户提供什么、需要先探测什么、需要哪些 grant/connector/backend。

## Procedure
1. 感知当前状态。
2. 打开/读取必要资源。
3. 执行工具动作。
4. 验证结果。
5. 失败升级或请求用户。

## Required Tool Evidence
哪些结论必须来自哪些工具结果。

## Failure Modes
常见失败和换路策略。

## Working Memory Rules
何时记录 checkpoint, 记录哪些字段。

## Output Contract
最终回答或 artifact 应包含什么。
```

### SkillResource

未来通过 `skill_resource_open` 加载:

- 长 SOP 文档。
- 脚本、模板、参考文件。
- compiled workflow。
- 示例和反例。
- 验收清单。

## 内置 SOP 规划

先补最小内置 SOP 集:

- `builtin.sop.memory_governance`: 记忆分层、No Execution No Memory、长期记忆写入规则。
- `builtin.sop.browser_research`: 浏览器 scan/navigate/execute_js、搜索结果深挖、来源核验。
- `builtin.sop.planning`: 复杂任务计划、工作目录/计划状态、验证门。
- `builtin.sop.delegation`: 子 Agent/工作台/并行任务拆分、状态轮询、取消/干预。
- `builtin.sop.review`: 技术评审只读范围、发现格式、测试建议。
- `builtin.sop.verification`: 端到端验收、工具证据、VERDICT。

这些可以先以 `SkillCard.instructions` 形式存在, 后续迁移为 `SkillResource`。

## 实现计划

### P0: 文档和契约

- 编写本文档。
- 更新 TODO, 明确核心上下文和 Skill/SOP 模板是 Daily Agent 阶段目标。
- 增加测试锁定默认上下文必须包含核心宪法、能力导航、渐进披露规则。

### P1: CoreContextProvider MVP

- 新增 `agent_kernel/context/core.py`。
- 提供 `CoreAgentContext`、`CapabilityNavigationEntry` 和 `CoreAgentContextProvider`。
- 在 `ContextAssembler._default_providers()` 中把 provider 插入 System/Policy 后或合并到 System/Policy 层。
- 默认上下文保持短小, 目标 < 800 tokens。

### P2: Skill/SOP 内置卡增强

- 将内置 atomic skills 增加 `trigger_keywords` 或在 metadata/description 中体现触发词。
- 新增内置 SOP skills, 或先扩展现有 `builtin.atomic.web_research` 等 instructions。
- Skill index 中暴露 procedure/ref/resource 提示, 但不默认加载完整 instructions。

### P3: Working Checkpoint Protocol

- 强化 `memory_checkpoint` schema 和 Skill 指令。
- Context 中明确提示何时 checkpoint。
- Daily Agent 工具回灌时保留 checkpoint 输出摘要。

### P4: SkillResource

- 实现 `SkillResource` domain/service/repository。
- 实现 `skill_resource_open`。
- 将长 SOP、脚本、模板与 `SkillCard` 分离。

## 验收标准

- 日常对话首轮上下文包含核心宪法、能力导航、Skill index, 且不包含长 SOP 全文。
- 用户说“用浏览器搜索今天深圳天气”, 模型能从核心索引发现 web research skill, 打开或使用相关工具, 而不是只说明能力。
- 用户说“起多个 agent 并行调研”, 模型能发现 collaboration/delegation skill, 创建工作台或请求必要 connector 信息。
- 复杂任务中模型会记录 working checkpoint, 并在失败多轮后探测或请求用户。
- 长期记忆只能来自工具验证后的事件/结果, 不把模型猜测沉淀为事实。
