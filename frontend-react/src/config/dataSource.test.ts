import { describe, expect, it, beforeEach, vi } from "vitest";
import {
  buildConfigFromPreset,
  DATA_SOURCE_PRESETS,
  DATA_SOURCE_STORAGE_KEY,
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
  });

  it("exposes exactly two live presets", () => {
    expect(DATA_SOURCE_PRESETS).toHaveLength(2);
    expect(DATA_SOURCE_PRESETS.map((p) => p.label)).toEqual([
      "Local Postgres",
      "Remote Postgres",
    ]);
    expect(DATA_SOURCE_PRESETS.every((p) => p.mode === "live")).toBe(true);
    expect(DATA_SOURCE_PRESETS.map((p) => p.id)).toEqual([
      PRESET_IDS.local,
      PRESET_IDS.postgres,
    ]);
  });

  it("builds local live preset", () => {
    const config = buildConfigFromPreset(PRESET_IDS.local);
    expect(config.mode).toBe("live");
    expect(config.label).toBe("Local Postgres");
    expect(config.apiBaseUrl).toBe("http://127.0.0.1:8000");
  });

  it("builds postgres preset with 127.0.0.1 fallback when env unset", () => {
    const config = buildConfigFromPreset(PRESET_IDS.postgres);
    expect(config.mode).toBe("live");
    expect(config.presetId).toBe(PRESET_IDS.postgres);
    expect(config.apiBaseUrl).toBe("http://127.0.0.1:8000");
  });

  it("postgres preset prefers VITE_PRAXIS_POSTGRES_API_BASE_URL over generic URL", () => {
    vi.stubEnv("VITE_PRAXIS_POSTGRES_API_BASE_URL", "https://postgres.api.test");
    vi.stubEnv("VITE_PRAXIS_API_BASE_URL", "https://generic.api.test");
    const config = buildConfigFromPreset(PRESET_IDS.postgres);
    expect(config.apiBaseUrl).toBe("https://postgres.api.test");
  });

  it("persists and restores config from localStorage", () => {
    const live = buildConfigFromPreset(PRESET_IDS.local);
    persistConfig(live);
    const restored = resolveInitialConfig();
    expect(restored.mode).toBe("live");
    expect(restored.apiBaseUrl).toBe("http://127.0.0.1:8000");
  });

  it("defaults to postgres preset when env API URL is set", () => {
    vi.stubEnv("VITE_PRAXIS_API_BASE_URL", "https://api.example.com");
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

  it("defaults to local postgres preset when no env URL is set", () => {
    const config = resolveInitialConfig();
    expect(config.mode).toBe("live");
    expect(config.presetId).toBe(PRESET_IDS.local);
    expect(config.apiBaseUrl).toBe("http://127.0.0.1:8000");
  });

  it("falls back to local preset for an unknown preset id", () => {
    const config = buildConfigFromPreset("nonexistent-preset");
    expect(config.mode).toBe("live");
    expect(config.presetId).toBe(PRESET_IDS.local);
    expect(config.apiBaseUrl).toBe("http://127.0.0.1:8000");
  });

  it("localStorage round-trip omits token", () => {
    persistConfig(buildConfigFromPreset(PRESET_IDS.local));
    const raw = localStorage.getItem(DATA_SOURCE_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(raw).not.toContain("apiToken");
  });
});

describe("resolveDataProvider", () => {
  it("returns api provider for local live config", () => {
    const provider = resolveDataProvider(buildConfigFromPreset(PRESET_IDS.local));
    expect(provider.listCandidates).toBeTypeOf("function");
  });

  it("returns api provider for remote postgres config", () => {
    const provider = resolveDataProvider(buildConfigFromPreset(PRESET_IDS.postgres));
    expect(provider.listCandidates).toBeTypeOf("function");
  });
});
