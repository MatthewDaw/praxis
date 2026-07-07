import { describe, expect, it } from "vitest";
import type { GraphEdge, GraphNode } from "../../types/graph";
import { getTopCenterViewport, layoutDagNodes } from "./graphLayout";

function node(id: string): GraphNode {
  return { id, label: id, state: "active", confidence: 1 };
}

describe("layoutDagNodes", () => {
  it("returns null when there are no dependency edges", () => {
    const nodes = [node("a"), node("b")];
    const edges: GraphEdge[] = [{ src: "a", dst: "b", kind: "similarity" }];
    expect(layoutDagNodes(nodes, edges)).toBeNull();
  });

  it("layers prerequisites above dependents by longest chain", () => {
    // a -> b -> c means c depends on b depends on a; a is the root (top).
    const nodes = [node("a"), node("b"), node("c")];
    const edges: GraphEdge[] = [
      { src: "a", dst: "b", kind: "depends" },
      { src: "b", dst: "c", kind: "depends" },
    ];

    const pos = layoutDagNodes(nodes, edges);

    expect(pos).not.toBeNull();
    expect(pos!.get("a")!.y).toBe(0);
    expect(pos!.get("b")!.y).toBe(170);
    expect(pos!.get("c")!.y).toBe(340);
  });

  it("places nodes with no dependency edges in rows below the DAG", () => {
    const nodes = [node("a"), node("b"), node("loner")];
    const edges: GraphEdge[] = [{ src: "a", dst: "b", kind: "depends" }];

    const pos = layoutDagNodes(nodes, edges);

    // DAG occupies rows 0 (a) and 1 (b); the isolated node lands on row 2.
    expect(pos!.get("a")!.y).toBe(0);
    expect(pos!.get("b")!.y).toBe(170);
    expect(pos!.get("loner")!.y).toBe(340);
  });
});

describe("getTopCenterViewport", () => {
  it("fits bounds to the top center of the canvas", () => {
    const viewport = getTopCenterViewport(
      { x: 100, y: 50, width: 400, height: 200 },
      {
        width: 1000,
        height: 600,
        minZoom: 1,
        maxZoom: 1,
        topPadding: 20,
        sidePadding: 50,
        bottomPadding: 80,
      },
    );

    expect(viewport).toEqual({ x: 200, y: -30, zoom: 1 });
  });

  it("keeps top-center alignment when a caller forces the zoom level", () => {
    const viewport = getTopCenterViewport(
      { x: 100, y: 50, width: 400, height: 200 },
      {
        width: 1000,
        height: 600,
        minZoom: 1.25,
        maxZoom: 1.25,
        topPadding: 20,
        sidePadding: 50,
        bottomPadding: 80,
      },
    );

    expect(viewport).toEqual({ x: 125, y: -42.5, zoom: 1.25 });
  });
});
