import { contractHeaders } from "./contract";
import type { ApiDataProviderAuth } from "./apiClient";
import { resolveToken } from "./apiClient";

/**
 * Minimal disabled-status check for the productivity kill switch (R39).
 *
 * Deliberately not the full typed `getProductivity(range)` client (R33, a
 * separate not-yet-built ticket) -- this reads only the one signal the
 * dashboard needs to hide/disable the Productivity tab without a redeploy:
 * whether the backend kill switch is set. Fails OPEN (returns `false`, i.e.
 * "not disabled") on any non-2xx response or network error, so a transient
 * outage or a 403 for a non-owner caller never hides a tab that is actually
 * live -- only an explicit `{"status": "disabled"}` body hides it.
 */
export async function isProductivityDisabled(
  apiBaseUrl: string,
  auth?: string | ApiDataProviderAuth,
): Promise<boolean> {
  try {
    const root = apiBaseUrl.replace(/\/$/, "");
    const { token, orgId } = await resolveToken(auth);
    const response = await fetch(`${root}/productivity?range=day`, {
      headers: contractHeaders(token, orgId),
    });
    if (!response.ok) return false;
    const body = (await response.json()) as { status?: string };
    return body?.status === "disabled";
  } catch {
    return false;
  }
}
