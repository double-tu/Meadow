import { ChevronDown, ChevronRight, Pause, RotateCcw, Send, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { MeadowApiClient } from "../../shared/api/client";
import type { ChatMessage, ChatSession, RuntimeEvent, ToolCallRecord } from "../../shared/api/types";
import { useAsyncResource } from "../../shared/lib/useAsyncResource";
import { Badge, Button, EmptyState, Panel, StatusDot } from "../../shared/ui";

type ChatPageProps = {
  api: MeadowApiClient;
};

export function ChatPage({ api }: ChatPageProps) {
  const sessions = useAsyncResource(() => api.getChatSessions(), [api]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [pendingMessages, setPendingMessages] = useState<Record<string, ChatMessage[]>>({});
  const activeSession = useMemo(
    () => sessions.data?.find((session) => session.session_id === activeSessionId) || sessions.data?.[0] || null,
    [activeSessionId, sessions.data],
  );
  const messages = useAsyncResource(
    async () => (activeSession ? api.getChatMessages(activeSession.session_id) : []),
    [api, activeSession?.session_id],
  );

  useEffect(() => {
    if (!activeSessionId && sessions.data?.[0]) setActiveSessionId(sessions.data[0].session_id);
  }, [activeSessionId, sessions.data]);

  async function ensureSession(): Promise<ChatSession> {
    if (activeSession) return activeSession;
    const created = await api.createChatSession();
    setActiveSessionId(created.session_id);
    await sessions.reload();
    return created;
  }

  async function sendMessage() {
    const content = draft.trim();
    if (!content || sending) return;
    setDraft("");
    setSending(true);
    let pendingSessionId: string | null = null;
    let pendingMessageId: string | null = null;
    try {
      const session = await ensureSession();
      const runId = `chat_run_${crypto.randomUUID().replaceAll("-", "")}`;
      setActiveRunId(runId);
      pendingSessionId = session.session_id;
      pendingMessageId = `pending_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
      const pendingUserMessage: ChatMessage = {
        message_id: pendingMessageId,
        session_id: session.session_id,
        role: "user",
        content,
        run_id: runId,
        metadata: { pending: true },
      };
      setPendingMessages((current) => ({
        ...current,
        [session.session_id]: [...(current[session.session_id] || []), pendingUserMessage],
      }));
      await api.sendChatMessage(session.session_id, content, runId);
      await Promise.all([sessions.reload(), messages.reload()]);
    } finally {
      if (pendingSessionId && pendingMessageId) {
        setPendingMessages((current) => removePendingMessage(current, pendingSessionId, pendingMessageId));
      }
      setSending(false);
      setActiveRunId(null);
    }
  }

  async function runSessionAction(action: "pause" | "retry" | "clear") {
    if (!activeSession) return;
    if (action === "pause") {
      await api.pauseChat(activeSession.session_id);
      setSending(false);
      setActiveRunId(null);
    }
    if (action === "retry") await api.retryChat(activeSession.session_id);
    if (action === "clear") await api.clearChat(activeSession.session_id);
    await Promise.all([sessions.reload(), messages.reload()]);
  }

  return (
    <div className="chat-grid">
      <Panel
        title="对话"
        actions={
          <Button
            variant="primary"
            onClick={async () => {
              const session = await api.createChatSession();
              setActiveSessionId(session.session_id);
              await sessions.reload();
            }}
          >
            新建
          </Button>
        }
      >
        <div className="session-list">
          {(sessions.data || []).map((session) => (
            <button
              className={`session-row ${session.session_id === activeSession?.session_id ? "active" : ""}`}
              key={session.session_id}
              type="button"
              onClick={() => setActiveSessionId(session.session_id)}
            >
              <span>
                <StatusDot status={session.status} />
                <strong>{session.title || "新的对话"}</strong>
              </span>
              <small>{session.message_count || 0} 条消息</small>
            </button>
          ))}
          {!sessions.loading && !sessions.data?.length ? <EmptyState title="暂无对话" /> : null}
        </div>
      </Panel>

      <Panel
        className="chat-main"
        title={activeSession?.title || "日常对话"}
        actions={
          <>
            <Button icon={<Pause size={14} />} onClick={() => void runSessionAction("pause")}>
              暂停
            </Button>
            <Button icon={<RotateCcw size={14} />} onClick={() => void runSessionAction("retry")}>
              重试
            </Button>
            <Button variant="danger" icon={<Trash2 size={14} />} onClick={() => void runSessionAction("clear")}>
              清空
            </Button>
          </>
        }
      >
        <div className="message-list">
          {messages.error ? <EmptyState title="消息加载失败" detail={messages.error} /> : null}
          {[...(messages.data || []), ...(activeSession ? pendingMessages[activeSession.session_id] || [] : [])].map((message) => (
            <MessageBubble key={message.message_id} message={message} />
          ))}
          {sending ? (
            <div className="message assistant">
              <small>Meadow</small>
              <p>执行中，正在等待 Agent 返回结果。</p>
              {activeRunId ? <RunActivityPanel api={api} runId={activeRunId} live /> : null}
            </div>
          ) : null}
          {!messages.loading && !messages.data?.length && !sending ? (
            <EmptyState title="直接输入日常问题或任务" detail="模型会根据上下文、Skill 和工具能力自行决定执行路径。" />
          ) : null}
        </div>
        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            void sendMessage();
          }}
        >
          <textarea
            placeholder="输入消息。Enter 发送，Shift+Enter 换行。"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void sendMessage();
              }
            }}
          />
          <Button variant="primary" icon={<Send size={15} />} disabled={sending}>
            发送
          </Button>
        </form>
      </Panel>

      <Panel title="上下文">
        <div className="inspector-stack">
          <ContextItem title="运行模式" value="Agent / Skill / MCP / Workflow / Control" />
          <ContextItem title="当前 Session" value={activeSession?.session_id || "未选择"} />
          <ContextItem
            title="最后 Run"
            value={String(activeRunId || activeSession?.metadata?.active_run_id || activeSession?.metadata?.last_run_id || "无")}
          />
          <Badge tone="blue">模型自主选择工具</Badge>
        </div>
      </Panel>
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  return (
    <div className={`message ${message.role}${message.metadata?.pending ? " pending" : ""}`}>
      <small>{message.role === "user" ? "你" : `Meadow${message.run_id ? ` · ${message.run_id}` : ""}`}</small>
      <p>{message.content}</p>
      {message.role === "assistant" ? <MessageRunDetails message={message} /> : null}
    </div>
  );
}

function MessageRunDetails({ message }: { message: ChatMessage }) {
  const dailyAgent = readDailyAgent(message.metadata);
  const toolCalls = Array.isArray(dailyAgent?.tool_calls) ? (dailyAgent.tool_calls as ToolCallRecord[]) : [];
  if (!dailyAgent && toolCalls.length === 0) return null;
  const status = typeof dailyAgent?.status === "string" ? dailyAgent.status : "completed";
  const turns = typeof dailyAgent?.turns === "number" ? dailyAgent.turns : null;
  return (
    <details className="run-details">
      <summary>
        <span>
          <ChevronRight className="summary-closed" size={14} />
          <ChevronDown className="summary-open" size={14} />
          运行过程
        </span>
        <Badge tone={status === "completed" ? "green" : status === "failed" ? "red" : "amber"}>
          {status}
          {turns ? ` · ${turns} 轮` : ""}
        </Badge>
      </summary>
      <ToolCallTimeline toolCalls={toolCalls} />
    </details>
  );
}

function RunActivityPanel({
  api,
  runId,
  compact = false,
  live = false,
}: {
  api: MeadowApiClient;
  runId: string;
  compact?: boolean;
  live?: boolean;
}) {
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCallRecord[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let pollCount = 0;
    async function load() {
      try {
        const [nextEvents, nextToolCalls] = await Promise.all([api.listEventsByRun(runId), api.listToolCalls(runId)]);
        if (cancelled) return;
        setEvents(nextEvents);
        setToolCalls(nextToolCalls);
        setError(null);
      } catch (event) {
        if (!cancelled) setError(event instanceof Error ? event.message : String(event));
      }
    }
    void load();
    if (!live) return () => {
      cancelled = true;
    };
    const interval = window.setInterval(() => {
      pollCount += 1;
      if (pollCount > 90) {
        window.clearInterval(interval);
        return;
      }
      void load();
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [api, live, runId]);

  const visibleEvents = events.slice(-6);
  return (
    <div className={compact ? "run-activity compact" : "run-activity"}>
      <div className="run-activity-head">
        <strong>{live ? "实时过程" : "运行过程"}</strong>
        <Badge tone={toolCalls.some((call) => call.status === "failed") ? "red" : toolCalls.length ? "blue" : "amber"}>
          {toolCalls.length ? `${toolCalls.length} 工具` : "等待工具"}
        </Badge>
      </div>
      {error ? <small className="muted-text">过程加载失败：{error}</small> : null}
      <ToolCallTimeline toolCalls={toolCalls} compact={compact} />
      {!compact && visibleEvents.length ? (
        <div className="run-event-mini-list">
          {visibleEvents.map((event) => (
            <div className="run-event-mini" key={event.event_id}>
              <span>{event.event_type}</span>
              <small>{event.emitted_at || ""}</small>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ToolCallTimeline({ toolCalls, compact = false }: { toolCalls: ToolCallRecord[]; compact?: boolean }) {
  if (!toolCalls.length) return <small className="muted-text">还没有工具调用。模型可能正在思考、打开 Skill 或等待首个结果。</small>;
  const visible = compact ? toolCalls.slice(-4) : toolCalls;
  return (
    <div className="tool-call-timeline">
      {visible.map((call, index) => (
        <details className="tool-call-row" key={call.tool_call_id || `${call.capability_id}_${index}`}>
          <summary>
            <span>
              <StatusDot status={toolStatus(call)} />
              <strong>{call.capability_id || call.tool_call_id || "tool_call"}</strong>
            </span>
            <Badge tone={toolStatusTone(call)}>{call.status || (call.error ? "failed" : "recorded")}</Badge>
          </summary>
          <div className="tool-call-body">
            {call.input ? <JsonPreview title="输入" value={call.input} /> : null}
            {call.output ? <JsonPreview title="输出" value={call.output} /> : null}
            {call.error ? <JsonPreview title="错误" value={call.error} /> : null}
          </div>
        </details>
      ))}
    </div>
  );
}

function JsonPreview({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="json-mini">
      <span>{title}</span>
      <pre>{compactJson(summarizeValue(value))}</pre>
    </div>
  );
}

function ContextItem({ title, value }: { title: string; value: string }) {
  return (
    <div className="context-item">
      <span>{title}</span>
      <strong>{value}</strong>
    </div>
  );
}

function removePendingMessage(
  messagesBySession: Record<string, ChatMessage[]>,
  sessionId: string,
  messageId: string,
): Record<string, ChatMessage[]> {
  const sessionMessages = messagesBySession[sessionId] || [];
  if (!sessionMessages.some((message) => message.message_id === messageId)) return messagesBySession;
  const remaining = sessionMessages.filter((message) => message.message_id !== messageId);
  const next = { ...messagesBySession };
  if (remaining.length) next[sessionId] = remaining;
  else delete next[sessionId];
  return next;
}

function readDailyAgent(metadata: Record<string, unknown> | undefined): Record<string, unknown> | null {
  const llmResult = metadata?.llm_result;
  if (!isRecord(llmResult)) return null;
  const dailyAgent = llmResult.daily_agent;
  return isRecord(dailyAgent) ? dailyAgent : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function compactJson(value: unknown): string {
  const text = JSON.stringify(value, null, 2);
  if (!text) return "";
  return text.length > 900 ? `${text.slice(0, 900)}\n...[truncated ${text.length - 900} chars]` : text;
}

function summarizeValue(value: unknown): unknown {
  if (!isRecord(value)) return value;
  if (isRecord(value.page)) {
    const page = value.page;
    return {
      active_target_id: value.active_target_id,
      page: {
        title: page.title,
        url: page.url,
        visible_cards: Array.isArray(page.visible_cards) ? page.visible_cards.slice(0, 8) : undefined,
        search_results: Array.isArray(page.search_results) ? page.search_results.slice(0, 5) : undefined,
        links: Array.isArray(page.links) ? page.links.slice(0, 5) : undefined,
        text_preview: typeof page.text === "string" ? page.text.slice(0, 360) : undefined,
      },
      targets_count: Array.isArray(value.targets) ? value.targets.length : undefined,
    };
  }
  if (typeof value.body === "string") {
    return { ...value, body: value.body.slice(0, 500), body_truncated: value.body.length > 500 };
  }
  return value;
}

function toolStatus(call: ToolCallRecord): string {
  if (call.status) return call.status;
  if (call.error) return "failed";
  return "completed";
}

function toolStatusTone(call: ToolCallRecord): "blue" | "green" | "red" | "amber" {
  const status = toolStatus(call);
  if (status === "succeeded" || status === "completed") return "green";
  if (status === "failed" || status === "error") return "red";
  if (status === "running" || status === "pending") return "blue";
  return "amber";
}
