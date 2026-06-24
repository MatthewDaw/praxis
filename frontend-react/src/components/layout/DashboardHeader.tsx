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
  /** Compact action buttons rendered under the subtitle (e.g. modal openers). */
  tools?: React.ReactNode;
  /** Primary section navigation rendered inside the header box. */
  tabs?: React.ReactNode;
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
  tools,
  tabs,
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
        {tools ? <div className="dashboard-header__tools">{tools}</div> : null}
        {tabs ? <div className="dashboard-header__tabs">{tabs}</div> : null}
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
      </div>
    </header>
  );
}
