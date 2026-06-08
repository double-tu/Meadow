import { PlugZap, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import type { NavItem, ViewId } from "../../app/navigation";
import { Button } from "../ui";

type AppShellProps = {
  navItems: NavItem[];
  activeView: ViewId;
  title: string;
  subtitle: string;
  apiUrl: string;
  onApiUrlChange: (url: string) => void;
  onViewChange: (view: ViewId) => void;
  onRefresh: () => void;
  children: ReactNode;
};

export function AppShell({
  navItems,
  activeView,
  title,
  subtitle,
  apiUrl,
  onApiUrlChange,
  onViewChange,
  onRefresh,
  children,
}: AppShellProps) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">M</div>
          <div>
            <strong>Meadow</strong>
            <span>Agent 工作台</span>
          </div>
        </div>
        <nav className="nav-list" aria-label="主导航">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={`nav-button ${activeView === item.id ? "active" : ""}`}
                type="button"
                onClick={() => onViewChange(item.id)}
              >
                <Icon size={16} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
      </aside>
      <main className="main">
        <header className="topbar">
          <div className="title-block">
            <h1>{title}</h1>
            <p>{subtitle}</p>
          </div>
          <div className="connection-bar">
            <PlugZap size={15} />
            <input
              aria-label="Meadow API"
              spellCheck={false}
              value={apiUrl}
              onChange={(event) => onApiUrlChange(event.target.value)}
            />
            <Button icon={<RefreshCw size={14} />} onClick={onRefresh}>
              刷新
            </Button>
          </div>
        </header>
        <section className="status-strip">
          <span>API {apiUrl}</span>
          <span>React/Vite</span>
          <span>API-first</span>
        </section>
        <section className="view">{children}</section>
      </main>
    </div>
  );
}
