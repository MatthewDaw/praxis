# PRAXIS Human Gate — Demo Script (Monica's Pillar)

**Duration:** ~2 minutes live · **Act:** Human approval makes knowledge promotion trustworthy.

## Setup (before recording)

```powershell
cd frontend-react
npm run dev
```

Open http://localhost:5173. For the current app, the default source is the Local
Postgres live preset (`http://localhost:8000`). Use the dashboard data-source
control to switch to mock fixtures if you are rehearsing without the local API.

For live API rehearsal: set env vars per [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md); reload the page after mutations.

## Beat 1 — Problem framing (10s)

> "Dominic's demo arc is dumb agent to smart agent. My pillar is the trust checkpoint in the middle: PRAXIS puts a human gate between distillation and injection."

Point at candidate list — note provenance on every row (`logs/...jsonl:line`).

## Beat 2 — Credibility review (30s)

1. Select **TypeScript Exhaustive Switch Pattern** (cand_1) in the global selector.
2. Open **Candidate detail** — show frequency / recency / breadth breakdown.
3. Scroll **Audit trail** — distilled → scored events with JSONL links.

> "This is Matthew's pipeline output at the review boundary. Every score decomposes into evidence the reviewer can challenge before promoting."

## Beat 3 — Human gate promotion (30s)

1. Filter **proposed**; pick any proposed candidate with low confidence if you want to show the warning.
2. Click **Approve** → **confirmation dialog** → if confidence < 50%, note the low-confidence warning → **Confirm approve**.
3. Success banner confirms approval; the row/detail state displays as approved (`active` in the API lifecycle).

> "Nothing reaches the knowledge graph without an explicit human promotion."

## Beat 4 — Contradiction resolution (30s)

1. Select **experimental_options Before Config Load** (cand_9).
2. Show side-by-side rival **Experimental Flags in config.nu**.
3. Click **Keep this** on the choice you want to preserve — the resolved contradiction leaves the active review queue.
4. If neither side is sufficient, use **Your own resolution** and click **Resolve with my answer**.

> "Contradictions surface as pairs, not silent conflicts in memory."

## Beat 5 — Measurement handoff (20s)

Click **Load eval data** to show the current eval handoff surface. The modal
loads eval scopes from the `knowledge/serve` eval endpoints when a live API is
configured.

> "My dashboard is the gate. Matthew owns the pipeline that creates and stores these candidates, and Dominic owns the dumb-agent versus smart-agent eval proof that promoted knowledge improves future runs."

## Closing line

> "I built the review surface that makes PRAXIS auditable: provenance on every lesson, confidence you can inspect, and promotions you control. I am handing off to the pipeline and eval pillars."

## Video capture checklist

- [ ] 1920×1080 window, light theme (default React)
- [ ] Zoom candidate detail panel before breakdown shot
- [ ] Capture confirmation dialog + low-confidence warning (if applicable) + success banner
- [ ] Optional: split-screen with JSONL log file for provenance punch-in
