import type { ReactNode } from "react";

type PanelProps = {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
};

export function Panel({ title, actions, children, className = "" }: PanelProps) {
  return (
    <section className={`panel ${className}`}>
      {title || actions ? (
        <header className="panel-header">
          {title ? <h2>{title}</h2> : <span />}
          {actions ? <div className="panel-actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className="panel-body">{children}</div>
    </section>
  );
}
