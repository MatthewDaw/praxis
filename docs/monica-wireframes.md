# Monica Peters — Dashboard Wireframes (Day 1-2)

**Author:** Monica Peters <monigarr@MoniGarr.com>  
**Created:** 2026-06-17  
**Status:** Day 2 shell complete; visual reference for team review.

## Overview
The dashboard implements the "human in the loop" gate for PRAXIS knowledge candidates. It follows the states: proposed → suggested → active with clear visual transitions, provenance on every item, and confidence/credibility score components (frequency, recency, breadth) with tooltips.

## Screen 1: Dashboard Shell + Candidate List (Day 2 Deliverable)
- Top nav: PRAXIS logo + "Human Gate" badge + user name (Monica Peters)
- Sidebar (future): Filters by state, search, user menu
- Main area: 
  - Header: "Knowledge Candidates" + state dropdown filter (All / Proposed / Suggested / Active)
  - List of cards:
    - Title (distilled lesson)
    - Content preview (truncated)
    - State badge (color-coded: yellow=proposed, blue=suggested, green=active)
    - Confidence bar (visual % + tooltip explaining freq/recency/breadth)
    - Provenance line: `logs/session-XXX.jsonl:127` (clickable to source in future)
    - Action buttons: Promote (→ active), Reject (remove)
- Keyboard: Tab to cards, Enter to select / open detail (future)
- Footer note: "Provenance-linked • Keyboard accessible • Ready for backend integration (Days 6-7)"

## Screen 2: Candidate Detail View (Day 3 target)
- Split or modal: Full content, full confidence breakdown with rationale, full provenance chain, contradiction list (side-by-side comparison cards per dashboard rule), resolution actions.
- Audit trail section linking back to originating JSONL lines.

## Design Notes
- Tailwind utility classes for rapid iteration
- Purple accent (#aa3bff) for brand consistency with PRAXIS
- High contrast for accessibility (WCAG AA+)
- Responsive: collapses to single column on mobile for demo readiness

## Next
- Day 3: Implement detail view + full score tooltips
- Days 6-7: Wire real API responses from Matthew's pipeline (preserve provenance)
- Day 8-10: Polish for live demo, add video capture of human gate flow

This wireframe ensures the UX supports the ≥50% correction reduction narrative and is interview-ready.
