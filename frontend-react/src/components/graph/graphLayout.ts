import type { CandidateState } from "../../types/candidate";
import type { GraphEdge, GraphEdgeKind, GraphNode } from "../../types/graph";

export function stateNodeColors(state: CandidateState): {
  bg: string;
  text: string;
  border: string;
} {
  switch (state) {
    case "proposed":
      return {
        bg: "var(--state-proposed-bg)",
        text: "var(--state-proposed-text)",
        border: "var(--state-proposed-border)",
      };
    case "active":
      return {
        bg: "var(--state-active-bg)",
        text: "var(--state-active-text)",
        border: "var(--state-active-border)",
      };
    case "rejected":
    case "unrecognized":
      return {
        bg: "var(--state-muted-bg)",
        text: "var(--state-muted-text)",
        border: "var(--state-muted-border)",
      };
    default: {
      const _exhaustive: never = state;
      throw new Error(`Unhandled state: ${_exhaustive}`);
    }
  }
}

export function layoutGraphNodes(nodes: GraphNode[]): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  const cols = Math.max(1, Math.ceil(Math.sqrt(nodes.length)));
  const xGap = 240;
  const yGap = 130;

  nodes.forEach((node, index) => {
    const col = index % cols;
    const row = Math.floor(index / cols);
    positions.set(node.id, { x: col * xGap, y: row * yGap });
  });

  return positions;
}

/**
 * Layered (Sugiyama-style) layout keyed off directional dependency edges:
 * an edge `src -> dst` means `dst` depends on `src`, so prerequisites sit
 * above their dependents. Each node's row is the longest prerequisite chain
 * ending at it (roots with no prerequisites are row 0). Nodes not touched by
 * any dependency edge are laid out in wrapped rows below the DAG.
 *
 * Returns null when there are no dependency edges to layer by, so callers can
 * fall back to the plain grid.
 */
export function layoutDagNodes(
  nodes: GraphNode[],
  edges: GraphEdge[],
  kind: GraphEdgeKind = "depends",
): Map<string, { x: number; y: number }> | null {
  const nodeIds = new Set(nodes.map((n) => n.id));
  const deps = edges.filter(
    (e) => e.kind === kind && nodeIds.has(e.src) && nodeIds.has(e.dst),
  );
  if (deps.length === 0) {
    return null;
  }

  // dst depends on the srcs — these are its prerequisites.
  const prereqs = new Map<string, string[]>();
  const connected = new Set<string>();
  for (const edge of deps) {
    const list = prereqs.get(edge.dst);
    if (list) {
      list.push(edge.src);
    } else {
      prereqs.set(edge.dst, [edge.src]);
    }
    connected.add(edge.src);
    connected.add(edge.dst);
  }

  // Longest-path depth, memoized, with a cycle guard (plans are acyclic, but
  // never hang if a bad edge slips through).
  const depthOf = new Map<string, number>();
  const visiting = new Set<string>();
  function depth(id: string): number {
    const cached = depthOf.get(id);
    if (cached !== undefined) {
      return cached;
    }
    if (visiting.has(id)) {
      return 0;
    }
    visiting.add(id);
    let d = 0;
    for (const prereq of prereqs.get(id) ?? []) {
      d = Math.max(d, depth(prereq) + 1);
    }
    visiting.delete(id);
    depthOf.set(id, d);
    return d;
  }

  const byLayer = new Map<number, string[]>();
  let maxLayer = 0;
  for (const node of nodes) {
    if (!connected.has(node.id)) {
      continue;
    }
    const layer = depth(node.id);
    maxLayer = Math.max(maxLayer, layer);
    const list = byLayer.get(layer);
    if (list) {
      list.push(node.id);
    } else {
      byLayer.set(layer, [node.id]);
    }
  }

  // Assemble rows: dependency layers top-to-bottom, then isolated nodes wrapped.
  const rows: string[][] = [];
  for (let layer = 0; layer <= maxLayer; layer += 1) {
    const list = byLayer.get(layer);
    if (list && list.length) {
      rows.push(list);
    }
  }
  const isolated = nodes.filter((n) => !connected.has(n.id)).map((n) => n.id);
  const isolatedPerRow = 6;
  for (let i = 0; i < isolated.length; i += isolatedPerRow) {
    rows.push(isolated.slice(i, i + isolatedPerRow));
  }

  const xGap = 260;
  const yGap = 170;
  const positions = new Map<string, { x: number; y: number }>();
  rows.forEach((row, r) => {
    row.forEach((id, i) => {
      // Center each row around x=0 (the viewport re-centers on fit).
      positions.set(id, { x: (i - (row.length - 1) / 2) * xGap, y: r * yGap });
    });
  });
  return positions;
}

export interface GraphBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface TopCenterViewportOptions {
  width: number;
  height: number;
  minZoom?: number;
  maxZoom?: number;
  topPadding?: number;
  sidePadding?: number;
  bottomPadding?: number;
}

export interface GraphViewport {
  x: number;
  y: number;
  zoom: number;
}

/** Fit all nodes in view, aligned to the top center of the canvas. */
export function getTopCenterViewport(
  bounds: GraphBounds,
  options: TopCenterViewportOptions,
): GraphViewport {
  const {
    width,
    height,
    minZoom = 0.2,
    maxZoom = 1,
    topPadding = 8,
    sidePadding = 20,
    bottomPadding = 72,
  } = options;

  if (bounds.width <= 0 || bounds.height <= 0 || width <= 0 || height <= 0) {
    return { x: 0, y: 0, zoom: 1 };
  }

  const availWidth = width - sidePadding * 2;
  const availHeight = height - topPadding - bottomPadding;
  const zoom = Math.min(
    maxZoom,
    Math.max(minZoom, Math.min(availWidth / bounds.width, availHeight / bounds.height)),
  );

  const boundsCenterX = bounds.x + bounds.width / 2;

  return {
    x: width / 2 - boundsCenterX * zoom,
    y: topPadding - bounds.y * zoom,
    zoom,
  };
}
