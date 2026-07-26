// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

// App reads its data-source/candidate/graph state through these hooks and its
// login/space identity through these auth contexts. Stubbing them keeps this
// test focused on tab-driven rendering (the acceptance condition under test)
// without touching the network or requiring the real <OrgGate>/<SpaceGate>
// providers App is normally mounted inside (see src/main.tsx).
vi.mock("./auth/OrgGate", () => ({
  useOrg: () => ({
    orgId: "test-org",
    orgName: "Test Org",
    userId: "test-user",
    email: "test@example.com",
    orgs: [],
    getToken: async () => undefined,
    signOut: async () => {},
    selectOrg: () => {},
    switchOrg: () => {},
    deleteAndSwitchOrg: async () => {},
    renameAndRefreshOrg: async () => {},
  }),
}));

vi.mock("./auth/SpaceGate", () => ({
  useSpace: () => ({
    spaceId: "test-space",
    spaces: [],
    selectSpace: () => {},
    createAndSelectSpace: async () => {},
    deleteAndDeselectSpace: async () => {},
    renameAndRefreshSpace: async () => {},
  }),
}));

vi.mock("./hooks/useDataSource", () => ({
  useDataSource: () => ({
    config: { mode: "mock", presetId: "test-mock", label: "Test mock" },
    mode: "mock",
    label: "Test mock",
    detail: undefined,
    ingestApiBaseUrl: undefined,
    applyConfig: () => {},
  }),
}));

vi.mock("./hooks/useApiHealth", () => ({
  useApiHealth: () => ({
    storeType: undefined,
    candidateCount: undefined,
    loading: false,
    error: undefined,
    refetch: () => {},
  }),
}));

vi.mock("./hooks/useCandidates", () => ({
  useCandidates: () => ({
    provider: {},
    candidates: [],
    loading: false,
    error: null,
    lastAction: null,
    clearLastAction: () => {},
    refresh: () => {},
    refreshCandidate: async () => {},
    promote: async () => {},
    reject: async () => {},
    resolveContradiction: async () => {},
    resolveContradictionCustom: async () => {},
    createCandidate: async () => {},
    updateCandidate: async () => {},
    deleteCandidate: async () => {},
  }),
  filterCandidates: (candidates: unknown[]) => candidates,
}));

vi.mock("./hooks/useGraph", () => ({
  useGraph: () => ({ graph: null, loading: false, error: null }),
}));

describe("App — Productivity tab", () => {
  it("switches to the productivity view, selects the tab, and hides the FilterBar", async () => {
    const user = userEvent.setup();
    render(<App />);

    const tab = screen.getByRole("tab", { name: "Productivity" });
    expect(tab).toHaveAttribute("aria-selected", "false");

    await user.click(tab);

    expect(tab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "Productivity" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Candidate filters")).not.toBeInTheDocument();
  });
});
