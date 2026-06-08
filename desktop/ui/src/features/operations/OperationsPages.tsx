import { GitBranch, Network, PlaySquare, ShieldCheck, TerminalSquare, Workflow } from "lucide-react";

import type { MeadowApiClient } from "../../shared/api/client";
import { useAsyncResource } from "../../shared/lib/useAsyncResource";
import { Badge, Button, Card, EmptyState, Panel, StatusDot } from "../../shared/ui";

type PageProps = {
  api: MeadowApiClient;
};

export function NodesPage({ api }: PageProps) {
  const workspaces = useAsyncResource(() => api.listWorkspaces(), [api]);
  return (
    <Panel title="节点流转" actions={<Button onClick={() => void workspaces.reload()}>刷新</Button>}>
      <div className="card-grid">
        {(workspaces.data || []).map((workspace) => (
          <Card key={workspace.workspace_id} title={workspace.title || workspace.workspace_id} meta={<Badge tone="blue">{workspace.workspace_id}</Badge>}>
            <MetricRow label="Runs" value={workspace.run_ids?.length || 0} />
            <MetricRow label="Artifacts" value={workspace.artifact_ids?.length || 0} />
            <MetricRow label="待审批" value={workspace.pending_approval_ids?.length || 0} />
            <MetricRow label="工具调用" value={workspace.active_tool_call_ids?.length || 0} />
            <div className="action-strip">
              <Button>查看</Button>
              <Button>停止</Button>
              <Button>重试</Button>
              <Button variant="danger">删除</Button>
            </div>
          </Card>
        ))}
        {!workspaces.loading && !workspaces.data?.length ? <EmptyState title="暂无工作区节点" /> : null}
      </div>
    </Panel>
  );
}

export function WorkflowPage() {
  return (
    <div className="page-grid">
      <Panel title="工作流与子工作流审核">
        <div className="visual-placeholder">
          <Workflow size={36} />
          <strong>GraphAdapter 预留</strong>
          <span>首版以节点列表和审核卡片承载，后续可接入 React Flow 或自研 Canvas。</span>
        </div>
      </Panel>
      <Panel title="审核队列">
        <div className="card-grid">
          {["输入检查", "旁路逻辑", "子工作流输出"].map((title) => (
            <Card key={title} title={title} meta={<Badge tone="amber">待设计</Badge>}>
              <p className="muted-text">展示输入、输出、diff、风险与批准/拒绝操作。</p>
              <div className="action-strip">
                <Button variant="primary">批准</Button>
                <Button variant="danger">拒绝</Button>
              </div>
            </Card>
          ))}
        </div>
      </Panel>
    </div>
  );
}

export function SkillsPage({ api }: PageProps) {
  const skills = useAsyncResource(() => api.listSkills(), [api]);
  return (
    <Panel title="Skill 管理">
      <div className="card-grid two">
        {(skills.data || []).map((skill) => (
          <Card key={skill.skill_id} title={skill.name} meta={<Badge tone={skill.status === "active" ? "green" : "neutral"}>{skill.status}</Badge>}>
            <p>{skill.description || "无描述"}</p>
            <small>{skill.when_to_use || "未配置触发条件"}</small>
            <div className="action-strip">
              <Button>编辑</Button>
              <Button>启用</Button>
              <Button variant="danger">弃用</Button>
            </div>
          </Card>
        ))}
      </div>
    </Panel>
  );
}

export function ApprovalsPage({ api }: PageProps) {
  const approvals = useAsyncResource(() => api.listApprovals(), [api]);
  return (
    <Panel title="审批中心">
      <div className="card-grid">
        {(approvals.data || []).map((approval) => (
          <Card key={approval.approval_id} title={approval.reason || approval.approval_id} meta={<Badge tone="amber">{approval.status}</Badge>}>
            <MetricRow label="Run" value={approval.run_id || "无"} />
            <MetricRow label="Target" value={`${approval.target_type || "unknown"}:${approval.target_id || "-"}`} />
            <div className="action-strip">
              <Button variant="primary">批准</Button>
              <Button variant="danger">拒绝</Button>
            </div>
          </Card>
        ))}
        {!approvals.loading && !approvals.data?.length ? <EmptyState title="暂无待审批项" /> : null}
      </div>
    </Panel>
  );
}

export function McpPage({ api }: PageProps) {
  const servers = useAsyncResource(() => api.listMcpServers(), [api]);
  return (
    <Panel title="MCP">
      <div className="card-grid">
        {(servers.data || []).map((server) => (
          <Card key={server.name} title={server.name} meta={<Badge tone={server.enabled ? "green" : "neutral"}>{server.enabled ? "启用" : "停用"}</Badge>}>
            <MetricRow label="Transport" value={server.transport?.type || "unknown"} />
            <MetricRow label="Agent" value={server.agent_types?.join(", ") || "全部"} />
            <div className="action-strip">
              <Button>编辑</Button>
              <Button>测试</Button>
              <Button variant="danger">删除</Button>
            </div>
          </Card>
        ))}
      </div>
    </Panel>
  );
}

export function TasksPage({ api }: PageProps) {
  const tasks = useAsyncResource(() => api.listScheduledTasks(), [api]);
  return (
    <Panel title="任务与计划任务">
      <div className="card-grid">
        {(tasks.data || []).map((task) => (
          <Card key={task.task_id} title={task.name} meta={<Badge tone={task.enabled ? "green" : "neutral"}>{task.enabled ? "启用" : "停用"}</Badge>}>
            <MetricRow label="Schedule" value={`${task.schedule_kind} ${task.schedule_value}`} />
            <MetricRow label="下次运行" value={task.next_run_at || "无"} />
            <MetricRow label="触发次数" value={task.trigger_count || 0} />
            <div className="action-strip">
              <Button>立即运行</Button>
              <Button>停用</Button>
              <Button variant="danger">删除</Button>
            </div>
          </Card>
        ))}
      </div>
    </Panel>
  );
}

export function ControlPage({ api }: PageProps) {
  const targets = useAsyncResource(() => api.listControlTargets(), [api]);
  return (
    <Panel title="控制台">
      <div className="split-view">
        <div className="card-grid">
          {(targets.data || []).map((target) => (
            <Card key={target.target_id} title={target.label || target.target_id} meta={<Badge tone="blue">{target.kind}</Badge>}>
              <MetricRow label="Target" value={target.target_id} />
              <div className="action-strip">
                <Button>Inspect</Button>
                <Button>Screenshot</Button>
                <Button>Dump UI</Button>
              </div>
            </Card>
          ))}
        </div>
        <div className="visual-placeholder">
          <TerminalSquare size={34} />
          <strong>Target Preview</strong>
          <span>后续展示浏览器摘要、截图、UI tree 和 artifact。</span>
        </div>
      </div>
    </Panel>
  );
}

export function EventsPage({ api }: PageProps) {
  const events = useAsyncResource(() => api.listEvents(), [api]);
  return (
    <Panel title="事件流" actions={<Button onClick={() => void events.reload()}>刷新</Button>}>
      <div className="event-list">
        {(events.data || []).slice(-80).map((event) => (
          <div className="event-row" key={event.event_id}>
            <StatusDot status={event.event_type} />
            <strong>{event.event_type}</strong>
            <span>{event.run_id || "global"}</span>
            <small>{event.event_id}</small>
          </div>
        ))}
      </div>
    </Panel>
  );
}

export function OperationsOverview() {
  const items = [
    { icon: GitBranch, title: "节点查看、流转、停止、重试、删除" },
    { icon: Workflow, title: "工作流、子工作流审核、旁路逻辑" },
    { icon: ShieldCheck, title: "审批、授权、取消工具调用" },
    { icon: Network, title: "MCP Server 与 Agent 绑定" },
    { icon: PlaySquare, title: "任务、计划任务、触发历史" },
    { icon: TerminalSquare, title: "浏览器、桌面、移动控制" },
  ];
  return (
    <div className="card-grid two">
      {items.map((item) => {
        const Icon = item.icon;
        return (
          <Card key={item.title} title={item.title}>
            <Icon size={22} />
          </Card>
        );
      })}
    </div>
  );
}

function MetricRow({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="metric-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
