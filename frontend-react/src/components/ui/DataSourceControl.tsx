import { useEffect, useState } from "react";
import {
  DATA_SOURCE_PRESETS,
  getDeployedApiBaseUrl,
  PRESET_IDS,
} from "../../config/dataSource";
import type { DataSourceConfig } from "../../config/dataSource";
import type { ApiStoreType } from "../../hooks/useApiHealth";

interface DataSourceControlProps {
  config: DataSourceConfig;
  storeType?: ApiStoreType;
  onLoad: (presetId: string, customApiBaseUrl?: string) => void;
}

export function DataSourceControl({
  config,
  storeType,
  onLoad,
}: DataSourceControlProps) {
  const [presetId, setPresetId] = useState(config.presetId);
  const [customUrl, setCustomUrl] = useState(
    config.presetId === PRESET_IDS.custom ? config.apiBaseUrl ?? "" : "",
  );

  useEffect(() => {
    setPresetId(config.presetId);
    if (config.presetId === PRESET_IDS.custom) {
      setCustomUrl(config.apiBaseUrl ?? "");
    }
  }, [config]);

  const selectedPreset = DATA_SOURCE_PRESETS.find((p) => p.id === presetId);
  const deployedUrl = getDeployedApiBaseUrl();
  const deployedDisabled = presetId === PRESET_IDS.deployed && !deployedUrl;
  const showJsonFallbackHint =
    config.mode === "live" &&
    storeType === "json" &&
    (presetId === PRESET_IDS.postgres || config.presetId === PRESET_IDS.postgres);

  function handlePresetChange(nextId: string) {
    setPresetId(nextId);
    if (nextId !== PRESET_IDS.custom) {
      setCustomUrl("");
    }
  }

  function handleLoad() {
    onLoad(
      presetId,
      presetId === PRESET_IDS.custom ? customUrl : undefined,
    );
  }

  function helpText(): string {
    if (deployedDisabled) {
      return "Deployed API requires VITE_PRAXIS_API_BASE_URL at build time.";
    }
    if (presetId === PRESET_IDS.postgres) {
      return `${selectedPreset?.helpText ?? ""} Dashboard reads via candidate-api-v1 → AWS RDS. No DB credentials in the browser.`;
    }
    return `${selectedPreset?.helpText ?? ""} Dashboard reads candidates via candidate-api-v1 — not DynamoDB or PostgreSQL directly.`;
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
            <option
              key={preset.id}
              value={preset.id}
              disabled={preset.id === PRESET_IDS.deployed && !deployedUrl}
            >
              {preset.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn secondary data-source-control__load"
          onClick={handleLoad}
          disabled={deployedDisabled}
        >
          Load data
        </button>
      </div>
      {presetId === PRESET_IDS.custom ? (
        <input
          type="url"
          className="data-source-control__input"
          placeholder="https://api.example.com"
          value={customUrl}
          onChange={(e) => setCustomUrl(e.target.value)}
          aria-label="Custom API base URL"
        />
      ) : null}
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
