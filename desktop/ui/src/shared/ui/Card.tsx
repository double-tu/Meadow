import type { ReactNode } from "react";

type CardProps = {
  title: string;
  meta?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
};

export function Card({ title, meta, children, footer }: CardProps) {
  return (
    <article className="card">
      <div className="card-head">
        <h3>{title}</h3>
        {meta}
      </div>
      {children ? <div className="card-body">{children}</div> : null}
      {footer ? <div className="card-footer">{footer}</div> : null}
    </article>
  );
}
