# Monica Peters — Dashboard Wireframes (Day 1-2)

**Author:** Monica Peters <monigarr@MoniGarr.com>  
**Created:** 2026-06-17  
**Status:** Day 2 shell complete — Streamlit implementation in `frontend/app.py`.

## Overview
The dashboard implements the "human in the loop" gate for PRAXIS knowledge candidates. It follows the states: proposed → suggested → active with clear visual transitions, provenance on every item, and confidence/credibility score components (frequency, recency, breadth) with tooltips.

Architecture source of truth: [CONFIDENTIAL_PRAXIS_Project_Plan.html](CONFIDENTIAL_PRAXIS_Project_Plan.html).

## Screen 1: Dashboard Shell + Candidate List (Day 2 Deliverable)
- **Header:** `st.title` — "Candidate Review Gate" + subtitle
- **Filters:** `st.text_input` search + `st.selectbox` state filter (All / proposed / suggested / active)
- **Table view tab:** `st.dataframe` with title, state, confidence (`ProgressColumn`), provenance, createdAt; action selectbox + Promote/Reject buttons
- **Card view tab:** `st.columns(3)` grid with `st.container(border=True)` per candidate:
  - Title (`st.subheader`)
  - State badge (color-coded markdown: orange=proposed, blue=suggested, green=active)
  - Confidence (`st.progress`)
  - Provenance line (`st.caption` with `` `path:line` ``)
  - Content preview (`st.write`)
  - Promote / Reject buttons per card
- **Keyboard / a11y:** Streamlit defaults for tab order; full keyboard polish targeted Days 8–10
- **Footer note:** Provenance-linked — ready for backend integration (Days 6–7)

## Screen 2: Candidate Detail View (Day 3 target)
- `st.expander` or dedicated detail panel: full content, full confidence breakdown with rationale, full provenance chain, contradiction list (side-by-side comparison per dashboard rule), resolution actions.
- Audit trail section linking back to originating JSONL lines.

## Design Notes
- Streamlit-native layout (`st.columns`, bordered containers, progress bars) — no custom CSS framework
- High contrast for accessibility (WCAG AA+) via Streamlit defaults + clear state color coding
- Responsive: card grid collapses naturally on narrow viewports

## Proposed candidate contract (pending Matthew review)

Target shape for Days 6–7 API integration. Mock data in `frontend/mock_data.py` is **not normalized yet** — confirm with Matthew before changing mocks or wiring the pipeline.

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Stable candidate id |
| `title` | string | Distilled lesson title |
| `content` | string | Full lesson body |
| `state` | `proposed` \| `suggested` \| `active` | Human-gate lifecycle |
| `confidence` | float 0–1 | Aggregate score (Days 3+ breakdown: freq/recency/breadth) |
| `provenance` | string | `logs/<file>.jsonl:<line>` |
| `createdAt` | ISO 8601 | Creation timestamp |

## Next
- Day 3: Implement detail view + full score tooltips
- Days 6-7: Wire real API responses from Matthew's pipeline (preserve provenance)
- Day 8-10: Polish for live demo, add video capture of human gate flow

This wireframe ensures the UX supports the ≥50% correction reduction narrative and is interview-ready.
