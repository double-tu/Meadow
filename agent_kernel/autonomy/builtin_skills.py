"""Built-in Skill cards that expose Meadow atomic capabilities."""

from __future__ import annotations

from dataclasses import replace

from agent_kernel.autonomy.skill_service import SkillService
from agent_kernel.domain.skill import SkillCard, SkillExecutionMode, SkillStatus


BUILTIN_ATOMIC_SKILLS: tuple[SkillCard, ...] = (
  SkillCard(
    skill_id="builtin.atomic.web_research",
    name="网页检索与资料获取",
    description="通过 HTTP 或浏览器控制获取网页、搜索结果、天气、新闻、文档等实时信息。",
    when_to_use="用户要求搜索、查询最新信息、打开网页、浏览网页、获取今天/当前/最近的信息时使用。",
    instructions=(
      "INDEX_HINT: 先 scan tabs；导航/搜索后持续携带 target_id；搜索结果页不是最终答案；动态推荐/最新帖子/Feed 页面用 "
      "browser_execute_js 刷新、滚动、抽取可见卡片短 JSON（title/text/url/author/time/metrics），不要反复全页 browser_scan。\n"
      "SOP: 1) 感知：先用 browser_scan(tabs_only=true) 查看可用标签页；用户明确要求浏览器时优先复用真实浏览器。"
      "2) 导航/搜索：探索性打开网站、搜索页或新资料页时优先调用 browser_navigate 且不传 target_id，"
      "让工具创建并归属一个新标签页；只有用户明确要求操作当前页，或已获得本任务创建/归属的 target_id 时，"
      "才在该 target_id 上导航或执行 location.href。"
      "导航工具返回 target_id 时，后续 browser_scan/browser_execute_js 必须继续携带同一 target_id，避免读到其他标签页。"
      "3) 读取：用 browser_scan 读取当前页；该工具会返回 page.text、links、search_results 等结构。"
      "4) 深挖：如果当前页是搜索结果页，不能把搜索页当最终答案；从 search_results 中选择与目标最相关的结果，"
      "继续打开至少 2 个结果页（只有 1 个可用结果时除外），分别读取正文。"
      "如果 search_results 为空但 page.text/links 中出现候选标题或 URL，用 browser_execute_js 从页面 DOM 精确抽取候选链接。"
      "5) 动态信息流：用户要求打开、刷新、查看推荐/最新帖子/Feed 时，不要反复全页 browser_scan；"
      "应在目标 target_id 上用 browser_execute_js 执行 refresh/scroll/click/read DOM 小脚本，"
      "从可见 article/card/link/img/time/like/comment 节点抽取短 JSON 数组，字段至少包含 title/text/url/author/time/metrics。"
      "一次抽取为空时先滚动或等待再抽取，仍为空才说明页面登录、反爬或结构不可读。"
      "6) 证据账本：每个来源都要记住 title/url/关键事实/不确定点；候选来源未打开前不要把它当证据。"
      "7) 核验：多个来源互相印证后总结，不确定处明确说明；如果轮次或权限不足，输出已查证据、未完成原因和下一步可恢复动作。"
      "8) 控制：需要点击、滚动、提取特定 DOM 或处理动态页面时，用 browser_execute_js；优先小脚本精准读取，少做全页扫描。"
      "失败时尝试下一个候选链接或换用 http_request。执行后基于工具结果回答，不要只说明自己可以做。"
    ),
    status=SkillStatus.ACTIVE,
    execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    recommended_tools=["http_request", "browser_scan", "browser_navigate", "browser_execute_js"],
    constraints=[
      "只请求与用户目标相关的 URL。",
      "不要访问需要用户授权的私密页面，除非用户明确要求并已授权。",
      "涉及具体个人时，只总结公开网页中的职业/公开活动/公开来源，不推断隐私信息。",
    ],
    failure_modes=["网络不可用", "浏览器控制后端未连接", "目标网页拒绝访问或返回空内容"],
  ),
  SkillCard(
    skill_id="builtin.atomic.workspace",
    name="工作区文件读写",
    description="读取、写入、追加或精确替换工作区内的文本文件。",
    when_to_use="用户要求查看文件、修改项目代码、生成文件、补丁编辑或保存资料时使用。",
    instructions=(
      "读取文件使用 workspace_read；创建或覆盖使用 workspace_write；"
      "小范围精确修改优先使用 workspace_patch。写入前确认路径属于工作区。"
    ),
    status=SkillStatus.ACTIVE,
    execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    recommended_tools=["workspace_read", "workspace_write", "workspace_patch"],
    constraints=["不要写入工作区外路径。", "修改前尽量读取上下文。"],
    failure_modes=["路径不在授权工作区", "补丁文本不唯一", "文件不存在"],
  ),
  SkillCard(
    skill_id="builtin.atomic.code_execution",
    name="代码执行与验证",
    description="运行短 Python 或 shell 片段，用于计算、验证、轻量脚本和诊断。",
    when_to_use="用户要求计算、执行验证、运行小脚本、检查命令输出或需要端到端验收时使用。",
    instructions="使用 code_execute 运行最小必要代码，设置合理 timeout，并把 stdout/stderr/returncode 纳入结论。",
    status=SkillStatus.ACTIVE,
    execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    recommended_tools=["code_execute"],
    constraints=["避免执行破坏性命令。", "长时间或高风险命令需要用户确认。"],
    failure_modes=["超时", "命令返回非零", "运行环境缺少依赖"],
  ),
  SkillCard(
    skill_id="builtin.atomic.desktop_mobile_control",
    name="浏览器桌面移动控制",
    description="检查或操作浏览器、桌面窗口、移动设备，包括截图、点击、键盘输入、UI 树读取。",
    when_to_use="用户要求连接当前浏览器、操作桌面应用、控制移动端、点击输入、读取屏幕或 UI 状态时使用。",
    instructions=(
      "先用只读能力检查目标：browser_scan、desktop_screenshot、desktop_dump_ui、"
      "mobile_screenshot、mobile_dump_ui；需要改变外部状态时再调用点击、按键、输入或导航工具。"
    ),
    status=SkillStatus.ACTIVE,
    execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    recommended_tools=[
      "browser_scan",
      "browser_navigate",
      "browser_execute_js",
      "desktop_screenshot",
      "desktop_dump_ui",
      "desktop_click",
      "desktop_key",
      "desktop_type_text",
      "mobile_screenshot",
      "mobile_dump_ui",
      "mobile_tap",
      "mobile_key",
      "mobile_type_text",
    ],
    constraints=["外部状态变更需遵循策略审批。", "坐标操作前尽量先截图或读取 UI。"],
    failure_modes=["控制后端未连接", "目标不存在", "坐标或 UI 目标不准确"],
  ),
  SkillCard(
    skill_id="builtin.atomic.agent_delegation",
    name="子 Agent 委派",
    description="把子任务委派给已配置的 Agent Connector，并查询或取消委派任务。",
    when_to_use="用户请求并行处理、交给特定 Agent、调用 codeg/codex/generic 类外部 Agent 或拆分复杂任务时使用。",
    instructions="使用 agent_delegate 创建子任务；需要等待结果时用 agent_delegation_status；用户要求停止时用 agent_cancel_delegation。",
    status=SkillStatus.ACTIVE,
    execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    recommended_tools=["agent_delegate", "agent_delegation_status", "agent_cancel_delegation"],
    constraints=["connector_id 必须来自系统已配置连接器。", "委派任务要保持明确、可执行。"],
    failure_modes=["连接器未配置", "子 Agent 无响应", "委派任务失败"],
  ),
  SkillCard(
    skill_id="builtin.atomic.collaboration_workbench",
    name="多 Agent 协作工作台",
    description="创建和推进群聊、多 CLI 协同、技术评审、并行子 Agent delegation 等异步协作空间。",
    when_to_use=(
      "用户要求组织多个 Agent/CLI 一起工作、技术评审、群聊讨论、主持人协调、"
      "持续任务、并行搜索、需要人和 Agent 混合参与时使用。"
    ),
    instructions=(
      "先用 workbench_create 创建合适类型的工作台：group_chat、cli_collaboration、"
      "technical_review 或 parallel_delegation。需要继续推进时用 workbench_message 代表主持人、"
      "用户代理或 reviewer 发消息；需要查看进度用 workbench_status；讨论形成结论后用 "
      "workbench_decision 生成决策 artifact；用户要求停止时用 workbench_cancel。"
      "如果缺少 connector_id、成员角色、评审目标等必要信息，使用 user_input_request 挂起向用户确认。"
    ),
    status=SkillStatus.ACTIVE,
    execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    recommended_tools=[
      "workbench_create",
      "workbench_status",
      "workbench_message",
      "workbench_decision",
      "workbench_cancel",
      "user_input_request",
      "agent_delegation_status",
    ],
    constraints=[
      "不要在 UI 或服务层用关键词硬路由，必须由模型基于上下文和 Skill 选择工具。",
      "能异步推进的任务应保留 workbench_id，后续通过 status/message/decision 继续。",
      "人类参与者可以直接进入工作台，也可以由日常 Agent 作为代理人转述和推进。",
    ],
    failure_modes=["连接器未配置", "成员或任务切片不完整", "子 Agent 长时间运行", "需要用户确认后才能继续"],
  ),
  SkillCard(
    skill_id="builtin.atomic.memory_checkpoint",
    name="记忆检查点与演化埋点",
    description="记录当前任务的关键上下文、经验或后续可沉淀为记忆/Skill 的候选内容。",
    when_to_use="长任务阶段性总结、用户偏好、流程经验、可复用技巧或需要后续记忆演化埋点时使用。",
    instructions="使用 memory_checkpoint 保存工作上下文；使用 memory_evolution_note 记录可结算的候选经验。",
    status=SkillStatus.ACTIVE,
    execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    recommended_tools=["memory_checkpoint", "memory_evolution_note"],
    constraints=["不要记录敏感凭据。", "只记录对后续任务有价值的压缩信息。"],
    failure_modes=["记忆服务未配置", "记录内容过泛或包含敏感信息"],
  ),
  SkillCard(
    skill_id="builtin.atomic.user_input",
    name="用户输入请求",
    description="当任务缺少必要信息或需要用户决策时，暂停并请求用户补充输入。",
    when_to_use="缺少关键参数、存在多个不可安全推断的选择、或继续执行前必须获得用户授权时使用。",
    instructions="用 user_input_request 提出一个清晰问题，必要时给出候选项；不要把可自行判断的问题都丢给用户。",
    status=SkillStatus.ACTIVE,
    execution_mode=SkillExecutionMode.AGENT_INTERPRETED,
    recommended_tools=["user_input_request"],
    constraints=["问题要短且可执行。"],
    failure_modes=["用户暂未回复"],
  ),
)


def ensure_builtin_atomic_skills(skill_service: SkillService) -> list[SkillCard]:
  """Create or refresh built-in atomic Skill cards without overriding user status."""

  ensured: list[SkillCard] = []
  for builtin in BUILTIN_ATOMIC_SKILLS:
    existing = skill_service.get(builtin.skill_id)
    if existing is None:
      skill_service.save(builtin)
      ensured.append(builtin)
      continue
    refreshed = replace(
      builtin,
      status=existing.status,
      procedure_memory_ref=existing.procedure_memory_ref,
      compiled_workflow_ref=existing.compiled_workflow_ref,
      examples=list(existing.examples),
    )
    skill_service.save(refreshed)
    ensured.append(refreshed)
  return ensured
