import type { CandidateState } from "../../types/candidate";
import type { GraphNode } from "../../types/graph";

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
    case "decayed":
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
