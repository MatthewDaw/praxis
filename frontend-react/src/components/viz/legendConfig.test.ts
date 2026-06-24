import { describe, expect, it } from "vitest";
import type { GraphEdgeKind } from "../../types/graph";
import {
  EDGE_LEGEND,
  FUNNEL_STATES,
  funnelStatesMatchConfig,
} from "./legendConfig";

const ALL_EDGE_KINDS: GraphEdgeKind[] = ["contradiction", "support", "similarity"];

describe("legendConfig", () => {
  it("defines legend entries for every graph edge kind", () => {
    const kinds = EDGE_LEGEND.map((entry) => entry.kind);
    expect(kinds).toEqual(ALL_EDGE_KINDS);
  });

  it("matches funnel states used by StateFunnel", () => {
    expect(funnelStatesMatchConfig(FUNNEL_STATES)).toBe(true);
    expect(FUNNEL_STATES).toEqual(["proposed", "active", "rejected"]);
  });

  it("provides unique edge kind labels", () => {
    const labels = EDGE_LEGEND.map((entry) => entry.label);
    expect(new Set(labels).size).toBe(labels.length);
  });
});
