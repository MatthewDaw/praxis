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
  const badgeClass = isLive ? "env-badge env-badge--live" : "env-badge env-badge--mock";
  const modeText = isLive ? "Live Data" : "Mock Data";
  const storeLabel = isLive ? storeSuffix(storeType) : undefined;
  const titleParts = [detail, storeLabel].filter(Boolean);

  return (
    <div className="env-badge-wrap">
      <span
        className={badgeClass}
        aria-live="polite"
        title={titleParts.length > 0 ? titleParts.join(" · ") : label}
      >
        {modeText}
      </span>
      <span className="env-badge-detail">
        {label}
        {detail ? (
          <>
            {" · "}
            <code>{detail}</code>
          </>
        ) : null}
        {storeLabel ? (
          <>
            {" · "}
            {storeLabel}
          </>
        ) : null}
      </span>
    </div>
  );
}
