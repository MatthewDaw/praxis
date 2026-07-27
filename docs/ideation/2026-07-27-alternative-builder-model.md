# Ideation — an alternative builder for long remote jobs (Claude token overflow)

- **Date:** 2026-07-27
- **Trigger:** ~76% of the weekly Claude Max limit consumed in two days of af-build runs; need an overflow builder.
- **Constraints (user):** pay-as-you-go only (NO subscription), roughly Claude-Max-comparable effective cost, near-Sonnet coding performance, survives multi-hour unattended runs.
- **Passes:** external provider/pricing research + codebase grounding on what af-build actually binds to.

---

## The finding that reframes the question

**A model swap costs zero code change. A harness swap is the expensive thing — and you don't need one.**

Codebase grounding (file:line):
- **No model is pinned anywhere in the build path.** `af-build/SKILL.md` (917 lines) contains no model id; neither do `hooks/*` or `agent_factory/.env`. `knowledge/serve/session_launcher.py:118-126` builds `claude --bg <cmd> --permission-mode bypassPermissions` and passes **no `--model`** — it takes whatever the CLI defaults to, overridable via `extra_args`. The only hardcoded ids are outside the build path (an LLM judge at `evals/plan_repro/llm_evaluator.py:51`, and optional per-eval-case pins).
- **The deterministic state machine is already harness-free.** `hooks/_praxis.py:36-47` and `hooks/_ticket_state.py:64-71` import **stdlib only**. Claims, leases, validation records, the plan gate — all plain HTTP + git + shell. That is the majority of the factory and it is portable today.
- **`session_launcher.py:86-171` is already the model-agnostic seam** — a thin wrapper over `claude --bg` / `agents --json|resume|terminate` with an **injectable `Runner` and injectable `cli` name** (`:88`). Every box-service module routes through it and only it. Swapping harness later = replacing one class.

So the cheap move is: **keep Claude Code, point it at a different model's Anthropic-compatible endpoint.** `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` — plugins, stdio MCP, Stop hooks, `bypassPermissions`, background sessions and worktree fan-out all keep working because *the harness never learns the model changed*.

---

## Candidates (prices per 1M in/out; research current 2026-07-27, aggregator-sourced — verify before committing)

| Model | Price in/out (cache read) | SWE-bench Verified | Anthropic endpoint | Note |
|---|---|---|---|---|
| **MiniMax M2.7** | **$0.30 / $1.20** (cache **$0.06**, auto) | ~78% | Native | Cheapest; auto-caching needs no config |
| **DeepSeek V4-Pro** | $0.435 / $0.87 (cache hit **$0.0036**) | **80.6%** | Native, but undocumented by Anthropic | Must use `deepseek-v4-pro`; older ids retired 2026-07-24 |
| **GLM-5 (Z.ai)** | $1.00 / $3.20 (cache $0.20) | 77.8% | Native — vendor claims closest to true Anthropic spec | Safest compatibility bet |
| **Kimi K2.7-code** | **$0.95 / $4.00** (cache **$0.19**) — OFFICIAL | ~80% (K2.6: 80.2%) | Native, official Claude Code docs | The Kimi to actually use |
| **Kimi K3** | **$3.00 / $15.00** (cache $0.30) — OFFICIAL | flagship | Native | ⚠️ **Identical to Sonnet 5 pricing — saves nothing** |
| Qwen3-Coder | $0.11–0.22 / $0.80 | unverified | Weakly evidenced; likely needs a proxy | Cheapest sticker, weakest compat evidence |

Reference bar: **Claude Sonnet 5 is $3/$15** per 1M. So these are ~3–20× cheaper per token.

**Subscription trap:** GLM, MiniMax and Kimi all also sell $10–80/mo "coding plans." Those violate the PAYG-only constraint — use **metered API console billing**, not the coding plan.

---

## Recommendation

1. **Primary — MiniMax M2.7.** ~10–20× cheaper than Sonnet, competitive benchmark, native endpoint, and the best cache economics ($0.06 cache reads, automatic). Zero harness changes.
2. **Runner-up — DeepSeek V4-Pro.** Highest benchmark of the set and near-free cache hits; endpoint is native but *not* endorsed by Anthropic, so treat compatibility as unproven until smoke-tested.
3. **Safest-compat — GLM-5** if either of the above shows tool-use flakiness. More expensive, reportedly the most faithful Anthropic-spec implementation.
4. **Kimi** — your instinct is defensible on quality (80.2%), but **pricing could not be confirmed**; check `platform.kimi.ai` before choosing it.

**On opencode:** it is a *tier-3 fallback*, not the expected path. It only becomes necessary if endpoint compatibility fails across all four providers. It would cost you the Stop-hook gate, plugin/skill loading, the `ce-*` review panel, and Workflow worktree fan-out — i.e. a harness rebuild — while the state machine (`_praxis`, `_ticket_state`, graded verify, resolve_preview, all git/shell checks) would survive unchanged.

---

## The one real risk: cache semantics, not correctness

An agentic loop re-reads context constantly, so **cache pricing dominates the bill**. Each provider implements `cache_control` and streaming tool-use independently, and the most-reported failure mode is a **silent cache miss** — everything works, the bill is 5–10× the estimate. Mitigation is a one-line check after switching:

> assert `usage.cache_read_input_tokens` > 0 on the second turn of a session.

Secondary risks: partial streaming-tool-use JSON accumulation, and providers whose "Anthropic compatibility" omits cache control entirely. `claude-code-router` or LiteLLM (`/v1/messages` shim) is the fallback if a native endpoint is flaky.

---

## Proposed next step (cheap, bounded)

Smoke-test **one** provider end-to-end before trusting it overnight:
1. `export ANTHROPIC_BASE_URL=... ANTHROPIC_AUTH_TOKEN=...` on the devbox.
2. Run `/af-build af-super-run` — 7 tickets remain, small and already-scoped, and it exercises the full stack: Stop-hook gate, MCP, plugins, worktree fan-out, graded checks.
3. Verify: gate blocks/releases correctly, `cache_read_input_tokens` > 0, tickets reach `finished`, no `wip: salvage` commits.

`af-super-run`'s 7 remaining tickets are the ideal canary — real work, bounded cost.

---

## Open decisions
- Which provider to smoke-test first (recommend MiniMax on price, GLM if you want compatibility certainty).
- Whether to verify Kimi pricing before deciding (your stated inclination).
- Whether to make `session_launcher`'s `cli`/`extra_args` configurable per-job now, so venue *and model* become job fields — cheap, and it is where the box-service already points.
