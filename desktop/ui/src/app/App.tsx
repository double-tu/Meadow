import { useMemo, useState } from "react";

import { ChatPage } from "../features/chat";
import { ConfigPage } from "../features/config";
import {
  ApprovalsPage,
  ControlPage,
  EventsPage,
  McpPage,
  NodesPage,
  SkillsPage,
  TasksPage,
  WorkflowPage,
} from "../features/operations";
import { MeadowApiClient, getStoredApiUrl, storeApiUrl } from "../shared/api/client";
import { AppShell } from "../shared/layout";
import { navigation, type ViewId } from "./navigation";

export function App() {
  const [activeView, setActiveView] = useState<ViewId>("chat");
  const [apiUrl, setApiUrl] = useState(getStoredApiUrl());
  const [refreshKey, setRefreshKey] = useState(0);
  const api = useMemo(() => new MeadowApiClient(apiUrl), [apiUrl, refreshKey]);
  const current = navigation.find((item) => item.id === activeView) || navigation[0];

  function updateApiUrl(next: string) {
    setApiUrl(storeApiUrl(next));
  }

  return (
    <AppShell
      navItems={navigation}
      activeView={activeView}
      title={current.label}
      subtitle={current.description}
      apiUrl={apiUrl}
      onApiUrlChange={updateApiUrl}
      onViewChange={setActiveView}
      onRefresh={() => setRefreshKey((key) => key + 1)}
    >
      <ViewRenderer view={activeView} api={api} key={`${activeView}:${refreshKey}`} />
    </AppShell>
  );
}

function ViewRenderer({ view, api }: { view: ViewId; api: MeadowApiClient }) {
  if (view === "chat") return <ChatPage api={api} />;
  if (view === "nodes") return <NodesPage api={api} />;
  if (view === "workflow") return <WorkflowPage />;
  if (view === "config") return <ConfigPage api={api} />;
  if (view === "skills") return <SkillsPage api={api} />;
  if (view === "approvals") return <ApprovalsPage api={api} />;
  if (view === "mcp") return <McpPage api={api} />;
  if (view === "tasks") return <TasksPage api={api} />;
  if (view === "control") return <ControlPage api={api} />;
  return <EventsPage api={api} />;
}
