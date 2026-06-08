import { Pause, RotateCcw, Send, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { MeadowApiClient } from "../../shared/api/client";
import type { ChatMessage, ChatSession } from "../../shared/api/types";
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
    try {
      const session = await ensureSession();
      await api.sendChatMessage(session.session_id, content);
      await Promise.all([sessions.reload(), messages.reload()]);
    } finally {
      setSending(false);
    }
  }

  async function runSessionAction(action: "pause" | "retry" | "clear") {
    if (!activeSession) return;
    if (action === "pause") await api.pauseChat(activeSession.session_id);
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
          {(messages.data || []).map((message) => (
            <MessageBubble key={message.message_id} message={message} />
          ))}
          {sending ? (
            <div className="message assistant">
              <small>Meadow</small>
              <p>执行中，正在等待 Agent 返回结果。</p>
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
          <ContextItem title="最后 Run" value={String(activeSession?.metadata?.last_run_id || activeSession?.metadata?.active_run_id || "无")} />
          <Badge tone="blue">模型自主选择工具</Badge>
        </div>
      </Panel>
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  return (
    <div className={`message ${message.role}`}>
      <small>{message.role === "user" ? "你" : `Meadow${message.run_id ? ` · ${message.run_id}` : ""}`}</small>
      <p>{message.content}</p>
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
