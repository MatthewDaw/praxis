import type { DataSourceMode } from "../../config/dataSource";
import type { ApiStoreType } from "../../hooks/useApiHealth";

interface EnvironmentBadgeProps {
  mode: DataSourceMode;
  label: string;
  detail?: string;
  storeType?: ApiStoreType;
}

function storeSuffix(storeType: ApiStoreType | undefined): string | undefined {
  if (storeType === "postgres") {
    return "PostgreSQL (RDS)";
  }
  if (storeType === "json") {
    return "JSON fallback";
  }
  return undefined;
}

export function EnvironmentBadge({
  mode,
  label,
  detail,
  storeType,
}: EnvironmentBadgeProps) {
  const isLive = mode === "live";
  const isLocalLogs = mode === "local-logs";
  const badgeClass = isLive
    ? "env-badge env-badge--live"
    : isLocalLogs
      ? "env-badge env-badge--local"
      : "env-badge env-badge--mock";
  const modeText = isLive ? "Live Data" : isLocalLogs ? "Local logs" : "Mock Data";
  const storeLabel = isLive ? storeSuffix(storeType) : undefined;
  const titleParts = [detail, storeLabel].filter(Boolean);

  const pillDetail = isLive ? detail : undefined;

  return (
    <div className="env-badge-wrap">
      <span
        className={badgeClass}
        aria-live="polite"
        title={titleParts.length > 0 ? titleParts.join(" · ") : label}
      >
        {modeText}
        {pillDetail ? (
          <>
            {" · "}
            <code className="env-badge__detail-code">{pillDetail}</code>
          </>
        ) : null}
      </span>
    </div>
  );
}
