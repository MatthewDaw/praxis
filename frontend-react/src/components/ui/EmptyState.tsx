import type { ReactNode } from "react";

interface EmptyStateProps {
  message: ReactNode;
}

export function EmptyState({ message }: EmptyStateProps) {
  return <div className="empty-state">{message}</div>;
}
