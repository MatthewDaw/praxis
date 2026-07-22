import { DataSourceControl } from "../ui/DataSourceControl";
import { OrgSwitcher } from "../ui/OrgSwitcher";
import { SpaceSwitcher } from "../ui/SpaceSwitcher";
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
  /** Snapshot quick-switch control, rendered directly below the space switcher. */
  snapshot?: React.ReactNode;
}

export function DashboardHeader({
  storeType,
  config,
  onDataSourceLoad,
  tools,
  tabs,
  snapshot,
}: DashboardHeaderProps) {
  return (
    <header className="dashboard-header">
      <div className="dashboard-header__brand">
        <div className="dashboard-header__brand-row">
          <h1 className="dashboard-header__title">praxis</h1>
          {tools ? <div className="dashboard-header__tools">{tools}</div> : null}
        </div>
        {tabs ? <div className="dashboard-header__tabs">{tabs}</div> : null}
      </div>
      <div className="dashboard-header__meta">
        <div className="dashboard-header__switchers">
          <DataSourceControl
            config={config}
            storeType={storeType}
            onLoad={onDataSourceLoad}
          />
          <OrgSwitcher />
          <SpaceSwitcher />
          {snapshot}
        </div>
      </div>
    </header>
  );
}
