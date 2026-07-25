// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { SectionTabs } from "./SectionTabs";

afterEach(cleanup);

describe("SectionTabs productivity kill switch (R39)", () => {
  it("renders the Productivity tab when the feature is not disabled", () => {
    render(
      <SectionTabs viewTab="table" contradictionCount={0} onViewTabChange={vi.fn()} />,
    );
    expect(screen.queryByRole("tab", { name: "Productivity" })).not.toBeNull();
  });

  it("hides the Productivity tab when the backend kill switch is set", () => {
    render(
      <SectionTabs
        viewTab="table"
        contradictionCount={0}
        onViewTabChange={vi.fn()}
        productivityDisabled
      />,
    );
    expect(screen.queryByRole("tab", { name: "Productivity" })).toBeNull();
  });

  it("never leaves the Productivity tab selectable if already active and disabled arrives", () => {
    const onViewTabChange = vi.fn();
    render(
      <SectionTabs
        viewTab="productivity"
        contradictionCount={0}
        onViewTabChange={onViewTabChange}
        productivityDisabled
      />,
    );
    expect(screen.queryByRole("tab", { name: "Productivity" })).toBeNull();
  });
});
