/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_PRAXIS_API_BASE_URL?: string;
  readonly VITE_PRAXIS_POSTGRES_API_BASE_URL?: string;
  readonly VITE_PRAXIS_API_TOKEN?: string;
  readonly VITE_PRAXIS_CONTRACT_VERSION?: string;
  readonly VITE_PRAXIS_EVAL_METRICS_URL?: string;
  readonly VITE_COGNITO_USER_POOL_ID?: string;
  readonly VITE_COGNITO_CLIENT_ID?: string;
  readonly VITE_COGNITO_REGION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
