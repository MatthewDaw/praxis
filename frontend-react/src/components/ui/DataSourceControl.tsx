import { useEffect, useState } from "react";
import { DATA_SOURCE_PRESETS, PRESET_IDS } from "../../config/dataSource";
import type { DataSourceConfig } from "../../config/dataSource";
import type { ApiStoreType } from "../../hooks/useApiHealth";
import type { LocalLogFileInput } from "../../types/transcript";

interface DataSourceControlProps {
  config: DataSourceConfig;
  storeType?: ApiStoreType;
  localSession?: { files: { name: string; lineCount: number }[] } | null;
  onLoad: (presetId: string, customApiBaseUrl?: string) => void;
  onLoadLocalLogs?: (files: LocalLogFileInput[]) => void;
  onClearLocalLogs?: () => void;
}

export function DataSourceControl({
  config,
  storeType,
  onLoad,
}: DataSourceControlProps) {
  const [presetId, setPresetId] = useState(config.presetId);

  useEffect(() => {
    setPresetId(config.presetId);
  }, [config]);

  const selectedPreset = DATA_SOURCE_PRESETS.find((p) => p.id === presetId);
  const showJsonFallbackHint =
    config.mode === "live" &&
    storeType === "json" &&
    (presetId === PRESET_IDS.postgres || config.presetId === PRESET_IDS.postgres);

  function handlePresetChange(nextId: string) {
    setPresetId(nextId);
    // Selecting a source switches servers immediately — no separate load step.
    onLoad(nextId);
  }

  function helpText(): string {
    return selectedPreset?.helpText ?? "";
  }

  return (
    <div className="data-source-control">
      <label className="data-source-control__label" htmlFor="data-source-preset">
        Data source
      </label>
      <div className="data-source-control__row">
        <select
          id="data-source-preset"
          className="data-source-control__select"
          value={presetId}
          onChange={(e) => handlePresetChange(e.target.value)}
          aria-describedby="data-source-help"
        >
          {DATA_SOURCE_PRESETS.map((preset) => (
            <option key={preset.id} value={preset.id}>
              {preset.label}
            </option>
          ))}
        </select>
      </div>
      <p className="data-source-control__hint" id="data-source-help">
        {helpText()}
      </p>
      {showJsonFallbackHint ? (
        <p className="data-source-control__hint data-source-control__hint--warn">
          API is using JSON fallback — set PRAXIS_DB_URL on the API for RDS persistence.
        </p>
      ) : null}
    </div>
  );
}
