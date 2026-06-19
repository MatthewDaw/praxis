export type DataSourceMode = "mock" | "live";

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
  evalMetricsUrl?: string;
  apiToken?: string;
  label: string;
}

export const DATA_SOURCE_STORAGE_KEY = "praxis-data-source-v1";

export const PRESET_IDS = {
  mock: "mock-fixtures",
  local: "live-local",
  postgres: "live-postgres-aws",
  deployed: "live-deployed",
  custom: "live-custom",
} as const;

export const DATA_SOURCE_PRESETS: DataSourcePreset[] = [
  {
    id: PRESET_IDS.mock,
    label: "Mock fixtures",
    mode: "mock",
    helpText:
      "Local JSON fixtures synced from frontend/mock_data.py — portfolio-safe demo.",
  },
  {
    id: PRESET_IDS.local,
    label: "Local API (Matthew)",
    mode: "live",
    defaultApiBaseUrl: "http://localhost:8000",
    helpText:
      "knowledge/serve FastAPI on localhost — JSON or Postgres depending on PRAXIS_DB_URL on the API.",
  },
  {
    id: PRESET_IDS.postgres,
    label: "Live API (PostgreSQL on AWS)",
    mode: "live",
    helpText:
      "Matthew's candidate API backed by AWS RDS (praxis_kg). Set PRAXIS_DB_URL on the API — dashboard uses API URL only.",
  },
  {
    id: PRESET_IDS.deployed,
    label: "Deployed API (Render)",
    mode: "live",
    helpText:
      "Build-time VITE_PRAXIS_API_BASE_URL — praxis-candidate-api on Render; ephemeral JSON unless PRAXIS_DB_URL is set on the API service.",
  },
  {
    id: PRESET_IDS.custom,
    label: "Custom API URL",
    mode: "live",
    helpText:
      "Any candidate-api-v1 server. API must allow CORS from this origin.",
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

function envEvalMetricsUrl(): string | undefined {
  return import.meta.env.VITE_PRAXIS_EVAL_METRICS_URL?.trim() || undefined;
}

export function deriveEvalMetricsUrl(apiBaseUrl: string): string {
  return `${apiBaseUrl.replace(/\/$/, "")}/metrics`;
}

export function getPresetById(presetId: string): DataSourcePreset | undefined {
  return DATA_SOURCE_PRESETS.find((p) => p.id === presetId);
}

export function buildConfigFromPreset(
  presetId: string,
  customApiBaseUrl?: string,
): DataSourceConfig {
  const preset = getPresetById(presetId);
  if (!preset) {
    return buildConfigFromPreset(PRESET_IDS.mock);
  }

  if (preset.mode === "mock") {
    return {
      mode: "mock",
      presetId: preset.id,
      label: preset.label,
      apiToken: envApiToken(),
    };
  }

  let apiBaseUrl: string | undefined;
  if (preset.id === PRESET_IDS.custom) {
    apiBaseUrl = customApiBaseUrl?.trim();
  } else if (preset.id === PRESET_IDS.deployed) {
    apiBaseUrl = envApiBaseUrl();
  } else if (preset.id === PRESET_IDS.postgres) {
    apiBaseUrl = envPostgresApiBaseUrl() ?? "http://localhost:8000";
  } else {
    apiBaseUrl = preset.defaultApiBaseUrl;
  }

  if (!apiBaseUrl) {
    return buildConfigFromPreset(PRESET_IDS.mock);
  }

  const normalized = apiBaseUrl.replace(/\/$/, "");
  const evalFromEnv = envEvalMetricsUrl();
  const evalMetricsUrl =
    evalFromEnv &&
    (preset.id === PRESET_IDS.deployed || preset.id === PRESET_IDS.postgres)
      ? evalFromEnv
      : deriveEvalMetricsUrl(normalized);

  return {
    mode: "live",
    presetId: preset.id,
    apiBaseUrl: normalized,
    evalMetricsUrl,
    apiToken: envApiToken(),
    label: preset.label,
  };
}

function isValidStoredConfig(value: unknown): value is DataSourceConfig {
  if (!value || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    record.mode === "mock" ||
    (record.mode === "live" && typeof record.apiBaseUrl === "string")
  );
}

export function resolveInitialConfig(): DataSourceConfig {
  try {
    const raw = localStorage.getItem(DATA_SOURCE_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (isValidStoredConfig(parsed)) {
        const stored = parsed as DataSourceConfig;
        if (stored.mode === "mock") {
          return {
            mode: "mock",
            presetId: stored.presetId || PRESET_IDS.mock,
            label: stored.label || "Mock fixtures",
            apiToken: envApiToken(),
          };
        }
        const apiBaseUrl = stored.apiBaseUrl!.replace(/\/$/, "");
        return {
          mode: "live",
          presetId: stored.presetId,
          apiBaseUrl,
          evalMetricsUrl:
            stored.evalMetricsUrl ?? deriveEvalMetricsUrl(apiBaseUrl),
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

  const deployedUrl = envApiBaseUrl();
  if (deployedUrl) {
    return buildConfigFromPreset(PRESET_IDS.postgres);
  }

  return buildConfigFromPreset(PRESET_IDS.mock);
}

export function persistConfig(config: DataSourceConfig): void {
  const toStore: DataSourceConfig = {
    mode: config.mode,
    presetId: config.presetId,
    label: config.label,
    apiBaseUrl: config.apiBaseUrl,
    evalMetricsUrl: config.evalMetricsUrl,
  };
  localStorage.setItem(DATA_SOURCE_STORAGE_KEY, JSON.stringify(toStore));
}

export function configDetail(config: DataSourceConfig): string | undefined {
  if (config.mode === "mock") {
    return "mock-candidates.json";
  }
  return config.apiBaseUrl;
}
