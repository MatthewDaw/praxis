export type DataSourceMode = "mock" | "live" | "local-logs";

export interface DataSourcePreset {
  id: string;
  label: string;
  mode: DataSourceMode;
  defaultApiBaseUrl?: string;
  helpText: string;
}

export interface DataSourceConfig {
  mode: DataSourceMode;
  presetId: string;
  apiBaseUrl?: string;
  apiToken?: string;
  label: string;
}

export const DATA_SOURCE_STORAGE_KEY = "praxis-data-source-v1";

export const PRESET_IDS = {
  // Retained for App.tsx local-log handling (no longer surfaced in the menu).
  localLogs: "local-claude-logs",
  local: "live-local",
  postgres: "live-postgres-aws",
} as const;

export const DATA_SOURCE_PRESETS: DataSourcePreset[] = [
  {
    id: PRESET_IDS.local,
    label: "Local Postgres",
    mode: "live",
    defaultApiBaseUrl: "http://127.0.0.1:8000",
    helpText:
      "knowledge/serve FastAPI on 127.0.0.1:8000 — Postgres-backed candidate API.",
  },
  {
    id: PRESET_IDS.postgres,
    label: "Remote Postgres",
    mode: "live",
    helpText:
      "Hosted knowledge/serve API — set VITE_PRAXIS_POSTGRES_API_BASE_URL for the remote Postgres endpoint.",
  },
];

function envApiBaseUrl(): string | undefined {
  return import.meta.env.VITE_PRAXIS_API_BASE_URL?.trim() || undefined;
}

function envPostgresApiBaseUrl(): string | undefined {
  return (
    import.meta.env.VITE_PRAXIS_POSTGRES_API_BASE_URL?.trim() ||
    import.meta.env.VITE_PRAXIS_API_BASE_URL?.trim() ||
    undefined
  );
}

export function getDeployedApiBaseUrl(): string | undefined {
  return envApiBaseUrl();
}

export function getPostgresApiBaseUrl(): string | undefined {
  return envPostgresApiBaseUrl();
}

function envApiToken(): string | undefined {
  return import.meta.env.VITE_PRAXIS_API_TOKEN?.trim() || undefined;
}

export function getPresetById(presetId: string): DataSourcePreset | undefined {
  return DATA_SOURCE_PRESETS.find((p) => p.id === presetId);
}

export function buildConfigFromPreset(
  presetId: string,
  customApiBaseUrl?: string,
): DataSourceConfig {
  void customApiBaseUrl;
  const preset = getPresetById(presetId);
  if (!preset) {
    return buildConfigFromPreset(PRESET_IDS.local);
  }

  let apiBaseUrl: string | undefined;
  if (preset.id === PRESET_IDS.postgres) {
    apiBaseUrl = envPostgresApiBaseUrl() ?? "http://127.0.0.1:8000";
  } else {
    apiBaseUrl = preset.defaultApiBaseUrl;
  }

  if (!apiBaseUrl) {
    apiBaseUrl = "http://127.0.0.1:8000";
  }

  const normalized = apiBaseUrl.replace(/\/$/, "");

  return {
    mode: "live",
    presetId: preset.id,
    apiBaseUrl: normalized,
    apiToken: envApiToken(),
    label: preset.label,
  };
}

function isValidStoredConfig(value: unknown): value is DataSourceConfig {
  if (!value || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return record.mode === "live" && typeof record.apiBaseUrl === "string";
}

export function resolveInitialConfig(): DataSourceConfig {
  try {
    const raw = localStorage.getItem(DATA_SOURCE_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (isValidStoredConfig(parsed)) {
        const stored = parsed as DataSourceConfig;
        const apiBaseUrl = stored.apiBaseUrl!.replace(/\/$/, "");
        return {
          mode: "live",
          presetId: stored.presetId,
          apiBaseUrl,
          apiToken: envApiToken(),
          label: stored.label || "Live API",
        };
      }
    }
  } catch {
    /* ignore corrupt storage */
  }

  if (envPostgresApiBaseUrl()) {
    return buildConfigFromPreset(PRESET_IDS.postgres);
  }

  return buildConfigFromPreset(PRESET_IDS.local);
}

export function persistConfig(config: DataSourceConfig): void {
  const toStore: DataSourceConfig = {
    mode: config.mode,
    presetId: config.presetId,
    label: config.label,
    apiBaseUrl: config.apiBaseUrl,
  };
  localStorage.setItem(DATA_SOURCE_STORAGE_KEY, JSON.stringify(toStore));
}

export function configDetail(
  config: DataSourceConfig,
  localSession?: { files: { name: string }[] } | null,
): string | undefined {
  if (config.mode === "mock") {
    return "mock-candidates.json";
  }
  if (config.mode === "local-logs") {
    if (localSession && localSession.files.length > 0) {
      return localSession.files.map((file) => file.name).join(", ");
    }
    return "No files loaded";
  }
  return config.apiBaseUrl;
}
