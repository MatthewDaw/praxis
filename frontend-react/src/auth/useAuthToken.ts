import { useCallback } from "react";
import { fetchAuthSession } from "aws-amplify/auth";

/**
 * Resolve the current Cognito ID token JWT, refreshing via Amplify as needed.
 * Returns undefined when there is no active session (e.g. mock/offline dev).
 */
export async function getToken(): Promise<string | undefined> {
  try {
    const session = await fetchAuthSession();
    return session.tokens?.idToken?.toString();
  } catch {
    return undefined;
  }
}

/**
 * Hook exposing a stable getToken() for API callers. Amplify handles refresh,
 * so each call returns a currently-valid ID token (or undefined when signed out).
 */
export function useAuthToken(): { getToken: () => Promise<string | undefined> } {
  return { getToken: useCallback(getToken, []) };
}
