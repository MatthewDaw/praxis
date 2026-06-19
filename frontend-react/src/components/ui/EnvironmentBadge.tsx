interface EnvironmentBadgeProps {
  apiUrl?: string;
}

export function EnvironmentBadge({ apiUrl }: EnvironmentBadgeProps) {
  if (apiUrl) {
    return (
      <span className="env-badge env-badge--live" title={apiUrl}>
        Live API · <code>{apiUrl}</code>
      </span>
    );
  }

  return (
    <span className="env-badge env-badge--mock">
      Mock mode — local fixtures only
    </span>
  );
}
