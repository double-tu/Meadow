export type ActionIntent = "view" | "create" | "update" | "delete" | "stop" | "retry" | "approve" | "reject";

export type ActionSpec = {
  id: string;
  label: string;
  intent: ActionIntent;
  danger?: boolean;
  requiresConfirm?: boolean;
  run: () => Promise<void> | void;
};
