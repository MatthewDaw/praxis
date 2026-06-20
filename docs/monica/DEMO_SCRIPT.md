# PRAXIS Human Gate — Demo Script (Monica's Pillar)

**Duration:** ~3 minutes live · **Act:** Human approval makes knowledge promotion trustworthy.

## Setup (before recording)

```powershell
cd frontend-react
npm run dev
```

Open http://localhost:5173 — confirm mock mode banner (no `VITE_PRAXIS_API_BASE_URL`).

For live API rehearsal: set env vars per [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md); reload the page after mutations.

## Beat 1 — Problem framing (20s)

> "Claude Code logs contain durable lessons, but nothing verifies them before they compound. PRAXIS puts a human gate between distillation and injection."

Point at candidate list — note provenance on every row (`logs/...jsonl:line`).

## Beat 2 — Credibility review (45s)

1. Select **TypeScript Exhaustive Switch Pattern** (cand_1) in the global selector.
2. Open **Candidate detail** — show frequency / recency / breadth breakdown.
3. Scroll **Audit trail** — distilled → scored events with JSONL links.

> "Every score decomposes into evidence the reviewer can challenge before promoting."

## Beat 3 — Contradiction resolution (45s)

1. Select **experimental_options Before Config Load** (cand_9).
2. Show side-by-side rival **Experimental Flags in config.nu**.
3. Click **Keep this candidate** — rival leaves queue; audit entry appended.
4. (Optional) Show **Defer** — both candidates remain for later review.

> "Contradictions surface as pairs, not silent conflicts in memory."

## Beat 4 — Human gate promotion (45s)

1. Filter **proposed**; pick any proposed candidate (or cand_12 with low confidence to show warning).
2. Click **Promote** → **confirmation dialog** → if confidence < 50%, note the low-confidence warning → **Confirm promote**.
3. Success banner shows `proposed → suggested`; promote again to **active** (Act 2 climax).

> "Nothing reaches the knowledge graph without an explicit human promotion."

## Beat 5 — Measurement hook (25s)

Expand **Eval metrics** — placeholder or live curve if `PRAXIS_EVAL_METRICS_URL` is set.

> "Dominic's harness proves correction rate falls after promoted knowledge injects — this dashboard is where humans decide what counts."

## Closing line

> "I built the review surface that makes PRAXIS auditable: provenance on every lesson, confidence you can inspect, and promotions you control."

## Video capture checklist

- [ ] 1920×1080 window, light theme (default React)
- [ ] Zoom candidate detail panel before breakdown shot
- [ ] Capture confirmation dialog + low-confidence warning (if applicable) + success banner
- [ ] Optional: split-screen with JSONL log file for provenance punch-in
