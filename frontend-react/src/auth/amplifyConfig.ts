import { Amplify } from "aws-amplify";

function envUserPoolId(): string | undefined {
  return import.meta.env.VITE_COGNITO_USER_POOL_ID?.trim() || undefined;
}

function envClientId(): string | undefined {
  return import.meta.env.VITE_COGNITO_CLIENT_ID?.trim() || undefined;
}

function envRegion(): string | undefined {
  return import.meta.env.VITE_COGNITO_REGION?.trim() || undefined;
}

export function isAmplifyConfigured(): boolean {
  return Boolean(envUserPoolId() && envClientId());
}

let configured = false;

/**
 * Configure Amplify Auth from the VITE_COGNITO_* build-time vars.
 * Safe to call multiple times; a no-op when the Cognito vars are absent
 * (mock/offline dev) so the app still boots without a user pool.
 */
export function configureAmplify(): void {
  if (configured) {
    return;
  }
  const userPoolId = envUserPoolId();
  const userPoolClientId = envClientId();
  if (!userPoolId || !userPoolClientId) {
    return;
  }
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId,
        userPoolClientId,
        ...(envRegion() ? { region: envRegion() } : {}),
      },
    },
  });
  configured = true;
}
