import { DataSourceControl } from "../ui/DataSourceControl";
import { EnvironmentBadge } from "../ui/EnvironmentBadge";
import { GitHubRepoLink } from "../ui/GitHubRepoLink";
import type { DataSourceConfig, DataSourceMode } from "../../config/dataSource";
import type { ApiStoreType } from "../../hooks/useApiHealth";
import type { LocalLogFileInput } from "../../types/transcript";

interface DashboardHeaderProps {
  mode: DataSourceMode;
  label: string;
  detail?: string;
  storeType?: ApiStoreType;
  config: DataSourceConfig;
  localSession?: { files: { name: string; lineCount: number }[] } | null;
  onDataSourceLoad: (presetId: string, customApiBaseUrl?: string) => void;
  onLoadLocalLogs?: (files: LocalLogFileInput[]) => void;
  onClearLocalLogs?: () => void;
  onRefresh: () => void;
}

export function DashboardHeader({
  mode,
  label,
  detail,
  storeType,
  config,
  localSession,
  onDataSourceLoad,
  onLoadLocalLogs,
  onClearLocalLogs,
  onRefresh,
}: DashboardHeaderProps) {
  return (
    <header className="dashboard-header">
      <div className="dashboard-header__brand">
        <p className="dashboard-header__eyebrow">PRAXIS</p>
        <div className="dashboard-header__title-row">
          <h1 className="dashboard-header__title">Candidate Review Gate</h1>
          <GitHubRepoLink />
        </div>
        <p className="dashboard-header__subtitle">
          Review and approve AI-learned knowledge facts from agent sessions.
        </p>
      </div>
      <div className="dashboard-header__meta">
        <EnvironmentBadge
          mode={mode}
          label={label}
          detail={detail}
          storeType={storeType}
        />
        <DataSourceControl
          config={config}
          storeType={storeType}
          localSession={localSession}
          onLoad={onDataSourceLoad}
          onLoadLocalLogs={onLoadLocalLogs}
          onClearLocalLogs={onClearLocalLogs}
        />
        <div className="dashboard-header__actions">
          <button type="button" className="btn primary" onClick={onRefresh}>
            Refresh data
          </button>
          <a
            className="contract-link"
            href="../docs/integration/candidate-api-v1.md"
            target="_blank"
            rel="noreferrer"
          >
            Contract: candidate-api-v1
          </a>
        </div>
      </div>
    </header>
  );
}
