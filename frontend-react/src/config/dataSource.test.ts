import { describe, expect, it, beforeEach, vi } from "vitest";
import {
  buildConfigFromPreset,
  DATA_SOURCE_STORAGE_KEY,
  deriveEvalMetricsUrl,
  persistConfig,
  PRESET_IDS,
  resolveInitialConfig,
} from "./dataSource";
import { resolveDataProvider } from "../api/providerFactory";

function createStorageMock(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.get(key) ?? null;
    },
    key(index: number) {
      return [...store.keys()][index] ?? null;
    },
    removeItem(key: string) {
      store.delete(key);
    },
    setItem(key: string, value: string) {
      store.set(key, value);
    },
  };
}

describe("dataSource config", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", createStorageMock());
    vi.stubEnv("VITE_PRAXIS_API_BASE_URL", "");
    vi.stubEnv("VITE_PRAXIS_POSTGRES_API_BASE_URL", "");
    vi.stubEnv("VITE_PRAXIS_API_TOKEN", "");
    vi.stubEnv("VITE_PRAXIS_EVAL_METRICS_URL", "");
  });

  it("builds mock preset config", () => {
    const config = buildConfigFromPreset(PRESET_IDS.mock);
    expect(config.mode).toBe("mock");
    expect(config.presetId).toBe(PRESET_IDS.mock);
    expect(config.label).toBe("Mock fixtures");
    expect(config.apiBaseUrl).toBeUndefined();
  });

  it("builds local live preset with derived metrics URL", () => {
    const config = buildConfigFromPreset(PRESET_IDS.local);
    expect(config.mode).toBe("live");
    expect(config.apiBaseUrl).toBe("http://localhost:8000");
    expect(config.evalMetricsUrl).toBe("http://localhost:8000/metrics");
  });

  it("builds postgres preset with localhost fallback when env unset", () => {
    const config = buildConfigFromPreset(PRESET_IDS.postgres);
    expect(config.mode).toBe("live");
    expect(config.presetId).toBe(PRESET_IDS.postgres);
    expect(config.apiBaseUrl).toBe("http://localhost:8000");
    expect(config.evalMetricsUrl).toBe("http://localhost:8000/metrics");
  });

  it("postgres preset prefers VITE_PRAXIS_POSTGRES_API_BASE_URL over generic URL", () => {
    vi.stubEnv("VITE_PRAXIS_POSTGRES_API_BASE_URL", "https://postgres.api.test");
    vi.stubEnv("VITE_PRAXIS_API_BASE_URL", "https://generic.api.test");
    const config = buildConfigFromPreset(PRESET_IDS.postgres);
    expect(config.apiBaseUrl).toBe("https://postgres.api.test");
  });

  it("derives eval metrics URL without trailing slash", () => {
    expect(deriveEvalMetricsUrl("http://localhost:8000/")).toBe(
      "http://localhost:8000/metrics",
    );
  });

  it("persists and restores config from localStorage", () => {
    const live = buildConfigFromPreset(PRESET_IDS.local);
    persistConfig(live);
    const restored = resolveInitialConfig();
    expect(restored.mode).toBe("live");
    expect(restored.apiBaseUrl).toBe("http://localhost:8000");
  });

  it("defaults to postgres preset when env API URL is set", () => {
    vi.stubEnv("VITE_PRAXIS_API_BASE_URL", "https://api.example.com");
    vi.stubEnv("VITE_PRAXIS_EVAL_METRICS_URL", "https://api.example.com/metrics");
    const config = resolveInitialConfig();
    expect(config.mode).toBe("live");
    expect(config.presetId).toBe(PRESET_IDS.postgres);
    expect(config.apiBaseUrl).toBe("https://api.example.com");
  });

  it("defaults to postgres preset when postgres env URL is set", () => {
    vi.stubEnv("VITE_PRAXIS_POSTGRES_API_BASE_URL", "https://postgres.example.com");
    const config = resolveInitialConfig();
    expect(config.presetId).toBe(PRESET_IDS.postgres);
    expect(config.apiBaseUrl).toBe("https://postgres.example.com");
  });

  it("custom preset requires URL", () => {
    const empty = buildConfigFromPreset(PRESET_IDS.custom);
    expect(empty.mode).toBe("mock");

    const custom = buildConfigFromPreset(
      PRESET_IDS.custom,
      "https://custom.api.test",
    );
    expect(custom.mode).toBe("live");
    expect(custom.apiBaseUrl).toBe("https://custom.api.test");
  });

  it("localStorage round-trip omits token", () => {
    persistConfig(buildConfigFromPreset(PRESET_IDS.local));
    const raw = localStorage.getItem(DATA_SOURCE_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(raw).not.toContain("apiToken");
  });
});

describe("resolveDataProvider", () => {
  it("returns mock provider for mock config", () => {
    const provider = resolveDataProvider(buildConfigFromPreset(PRESET_IDS.mock));
    expect(provider.listCandidates).toBeTypeOf("function");
  });

  it("returns api provider for live config", () => {
    const provider = resolveDataProvider(buildConfigFromPreset(PRESET_IDS.local));
    expect(provider.getEvalMetrics).toBeTypeOf("function");
  });
});
