import { useEffect, useState } from "react";
import {
  DATA_SOURCE_PRESETS,
  getDeployedApiBaseUrl,
  PRESET_IDS,
} from "../../config/dataSource";
import type { DataSourceConfig } from "../../config/dataSource";
import type { ApiStoreType } from "../../hooks/useApiHealth";
import type { LocalLogFileInput } from "../../types/transcript";

interface DataSourceControlProps {
  config: DataSourceConfig;
  storeType?: ApiStoreType;
  localSession?: { files: { name: string; lineCount: number }[] } | null;
  onLoad: (presetId: string, customApiBaseUrl?: string) => void;
  onRegenerate?: (presetId: string) => void;
  regeneratePending?: boolean;
  onLoadLocalLogs?: (files: LocalLogFileInput[]) => void;
  onClearLocalLogs?: () => void;
}

export function DataSourceControl({
  config,
  storeType,
  localSession,
  onLoad,
  onRegenerate,
  regeneratePending = false,
  onLoadLocalLogs,
  onClearLocalLogs,
}: DataSourceControlProps) {
  const [presetId, setPresetId] = useState(config.presetId);
  const [regeneratePreset, setRegeneratePreset] = useState("offline-fake");
  const [customUrl, setCustomUrl] = useState(
    config.presetId === PRESET_IDS.custom ? config.apiBaseUrl ?? "" : "",
  );
  const [pendingFiles, setPendingFiles] = useState<FileList | null>(null);

  useEffect(() => {
    setPresetId(config.presetId);
    if (config.presetId === PRESET_IDS.custom) {
      setCustomUrl(config.apiBaseUrl ?? "");
    }
  }, [config]);

  const selectedPreset = DATA_SOURCE_PRESETS.find((p) => p.id === presetId);
  const deployedUrl = getDeployedApiBaseUrl();
  const deployedDisabled = presetId === PRESET_IDS.deployed && !deployedUrl;
  const isLocalLogsPreset = presetId === PRESET_IDS.localLogs;
  const canRegenerate = config.mode === "live" && Boolean(config.apiBaseUrl) && Boolean(onRegenerate);
  const showJsonFallbackHint =
    config.mode === "live" &&
    storeType === "json" &&
    (presetId === PRESET_IDS.postgres || config.presetId === PRESET_IDS.postgres);

  function handlePresetChange(nextId: string) {
    setPresetId(nextId);
    if (nextId !== PRESET_IDS.custom) {
      setCustomUrl("");
    }
    if (nextId !== PRESET_IDS.localLogs) {
      setPendingFiles(null);
    }
  }

  function handleLoad() {
    onLoad(
      presetId,
      presetId === PRESET_IDS.custom ? customUrl : undefined,
    );
  }

  async function handleLoadLocalLogs() {
    if (!pendingFiles || pendingFiles.length === 0 || !onLoadLocalLogs) {
      return;
    }
    const inputs: LocalLogFileInput[] = [];
    for (const file of Array.from(pendingFiles)) {
      inputs.push({ name: file.name, content: await file.text() });
    }
    onLoadLocalLogs(inputs);
    setPendingFiles(null);
  }

  function helpText(): string {
    if (isLocalLogsPreset) {
      return selectedPreset?.helpText ?? "";
    }
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
      {canRegenerate ? (
        <div className="data-source-control__row data-source-control__regenerate">
          <select
            className="data-source-control__select"
            value={regeneratePreset}
            onChange={(event) => setRegeneratePreset(event.target.value)}
            aria-label="Eval regeneration preset"
          >
            <option value="offline-fake">Offline fake</option>
            <option value="openrouter">OpenRouter</option>
          </select>
          <button
            type="button"
            className="btn secondary data-source-control__load"
            onClick={() => onRegenerate?.(regeneratePreset)}
            disabled={regeneratePending}
          >
            {regeneratePending ? "Regenerating" : "Regenerate evals"}
          </button>
        </div>
      ) : null}
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
      {isLocalLogsPreset ? (
        <div className="data-source-control__local-logs">
          <input
            type="file"
            accept=".jsonl,application/json"
            multiple
            onChange={(event) => setPendingFiles(event.target.files)}
            aria-label="Upload Claude Code JSONL session files"
          />
          <div className="data-source-control__row">
            <button
              type="button"
              className="btn secondary"
              onClick={() => void handleLoadLocalLogs()}
              disabled={!pendingFiles || pendingFiles.length === 0}
            >
              Load logs
            </button>
            {localSession && localSession.files.length > 0 ? (
              <button
                type="button"
                className="btn secondary"
                onClick={onClearLocalLogs}
              >
                Clear
              </button>
            ) : null}
          </div>
          {localSession && localSession.files.length > 0 ? (
            <ul className="data-source-control__file-list">
              {localSession.files.map((file) => (
                <li key={file.name}>
                  <code>{file.name}</code> — {file.lineCount} transcript lines
                </li>
              ))}
            </ul>
          ) : null}
        </div>
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
