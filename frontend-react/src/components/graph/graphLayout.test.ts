import { describe, expect, it } from "vitest";
import { getTopCenterViewport } from "./graphLayout";

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
