/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_PRAXIS_API_BASE_URL?: string;
  readonly VITE_PRAXIS_POSTGRES_API_BASE_URL?: string;
  readonly VITE_PRAXIS_API_TOKEN?: string;
  readonly VITE_PRAXIS_CONTRACT_VERSION?: string;
  readonly VITE_PRAXIS_EVAL_METRICS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
