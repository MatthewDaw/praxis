"""Mock knowledge candidates for the PRAXIS human-gate dashboard.

Provenance uses canonical form ``logs/<file>.jsonl:<line>``. Rows cand_6–cand_17
simulate pipeline distillation from Claude Code JSONL sessions on nushell/nushell.
Rows with ``evalCaseId`` align with ``knowledge/evals/cases/`` (see MATTHEW_HANDOFF.md).
Auto-generated ``eval_*`` rows cover every other registered case via ``eval_mock_bridge``.
"""


def get_demo_candidate_dicts() -> list[dict]:
    """Return hand-crafted demo narrative candidates (Acts 1–2 rehearsal)."""
    return [
        {
            "id": "cand_1",
            "evalCaseId": "quirky_exhaustive_switch",
            "evalCaseNamespace": "quirky",
            "title": "TypeScript Exhaustive Switch Pattern",
            "content": "When using a switch statement on a discriminated union or enum, include a default case that assigns the value to a variable of type `never`. This ensures the compiler will throw an error if a new variant is added to the union but not handled in the switch.",
            "state": "proposed",
            "confidence": 0.85,
            "provenance": "logs/session_20260615.jsonl:88",
            "createdAt": "2026-06-15T14:30:00Z",
            "scope": "frontend/typescript",
            "category": "pattern",
            "traceId": "phx-trace-exhaustive-switch",
            "sessionId": "session_20260615",
            "confidenceBreakdown": {
                "frequency": 0.82,
                "recency": 0.88,
                "breadth": 0.76,
                "frequencyRationale": "Seen in 9 of 11 TypeScript sessions with union-heavy refactors",
                "recencyRationale": "Last observed 2026-06-15 during exhaustive-switch MR review",
                "breadthRationale": "Applies across frontend, CLI, and shared types packages",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-15T14:30:00Z",
                    "provenance": "logs/session_20260615.jsonl:88",
                    "actor": "pipeline",
                },
                {
                    "action": "scored",
                    "timestamp": "2026-06-15T14:31:00Z",
                    "provenance": "logs/session_20260615.jsonl:88",
                    "actor": "pipeline",
                },
            ],
        },
        {
            "id": "cand_2",
            "title": "React useEffect Cleanup",
            "content": "Always return a cleanup function from useEffect when subscribing to external events or setting up intervals. This prevents memory leaks and unexpected behavior when components unmount.",
            "state": "suggested",
            "confidence": 0.92,
            "provenance": "logs/session_20260614.jsonl:214",
            "createdAt": "2026-06-14T09:15:00Z",
            "scope": "frontend/react",
            "category": "pattern",
            "traceId": "phx-trace-useeffect-cleanup",
            "sessionId": "session_20260614",
            "confidenceBreakdown": {
                "frequency": 0.91,
                "recency": 0.93,
                "breadth": 0.89,
                "frequencyRationale": "Repeated in 14 React debugging sessions over two weeks",
                "recencyRationale": "Observed 2026-06-14 after interval leak repro",
                "breadthRationale": "Hooks, subscriptions, and third-party SDK integrations",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-14T09:15:00Z",
                    "provenance": "logs/session_20260614.jsonl:214",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_suggested",
                    "timestamp": "2026-06-14T16:00:00Z",
                    "provenance": "logs/session_20260614.jsonl:214",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_3",
            "title": "GitLab CI Artifact Expiration",
            "content": "Set an explicit `expire_in` value for all GitLab CI artifacts to prevent storage bloat. A good default is '1 week' for temporary build artifacts.",
            "state": "active",
            "confidence": 0.98,
            "provenance": "logs/session_20260610.jsonl:52",
            "createdAt": "2026-06-10T11:45:00Z",
            "scope": "infra/ci",
            "category": "constraint",
            "traceId": "phx-trace-ci-artifact-expiry",
            "sessionId": "session_20260610",
            "confidenceBreakdown": {
                "frequency": 0.97,
                "recency": 0.95,
                "breadth": 0.99,
                "frequencyRationale": "Consistent CI hygiene correction across 6 repos",
                "recencyRationale": "Still referenced in June 2026 pipeline reviews",
                "breadthRationale": "GitLab CI jobs in backend, frontend, and infra repos",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-10T11:45:00Z",
                    "provenance": "logs/session_20260610.jsonl:52",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_active",
                    "timestamp": "2026-06-11T09:00:00Z",
                    "provenance": "logs/session_20260610.jsonl:52",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_4",
            "title": "Python Type Hinting for Dicts",
            "content": "Use `Dict[str, Any]` instead of `dict` when typing dictionaries with string keys and mixed value types to provide better IDE support and static analysis.",
            "state": "proposed",
            "confidence": 0.75,
            "provenance": "logs/session_20260616.jsonl:167",
            "createdAt": "2026-06-16T16:20:00Z",
            "scope": "backend/python",
            "category": "pattern",
            "confidenceBreakdown": {
                "frequency": 0.71,
                "recency": 0.78,
                "breadth": 0.74,
                "frequencyRationale": "Typed-dict guidance repeated in 5 Python review sessions",
                "recencyRationale": "Last cited 2026-06-16 during mypy strict-mode cleanup",
                "breadthRationale": "Applies to services, eval harness, and pipeline modules",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-16T16:20:00Z",
                    "provenance": "logs/session_20260616.jsonl:167",
                    "actor": "pipeline",
                },
                {
                    "action": "scored",
                    "timestamp": "2026-06-16T16:21:00Z",
                    "provenance": "logs/session_20260616.jsonl:167",
                    "actor": "pipeline",
                },
            ],
        },
        {
            "id": "cand_5",
            "title": "React Client State Persistence",
            "content": "Use component state or a small context store to persist selection and filter values across re-renders. Lift shared gate state to the page shell so list and detail panes stay in sync without duplicating API calls.",
            "state": "suggested",
            "confidence": 0.88,
            "provenance": "logs/session_20260617.jsonl:33",
            "createdAt": "2026-06-17T10:05:00Z",
            "scope": "frontend/react",
            "category": "pattern",
            "confidenceBreakdown": {
                "frequency": 0.86,
                "recency": 0.90,
                "breadth": 0.85,
                "frequencyRationale": "Client-state pattern in 7 React dashboard iterations",
                "recencyRationale": "Observed 2026-06-17 during human-gate filter refactor",
                "breadthRationale": "List, detail, and contradiction panels all rely on shared page state",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-17T10:05:00Z",
                    "provenance": "logs/session_20260617.jsonl:33",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_suggested",
                    "timestamp": "2026-06-17T14:00:00Z",
                    "provenance": "logs/session_20260617.jsonl:33",
                    "actor": "human-gate",
                },
            ],
        },
        # --- nushell/nushell session-derived candidates (mock) ---
        {
            "id": "cand_6",
            "title": "Nu Parser: Bitwise vs Pipeline Precedence",
            "content": "When editing the nushell parser or writing Nu expressions that mix bitwise and pipeline operators, remember that `&` binds tighter than `|`. Wrap pipeline sub-expressions in parentheses when combining them with bitwise ops, or precedence bugs will surface only in nested cases.",
            "state": "proposed",
            "confidence": 0.91,
            "provenance": "logs/nushell_contrib_20260503.jsonl:142",
            "createdAt": "2026-05-03T18:40:00Z",
            "confidenceBreakdown": {
                "frequency": 0.89,
                "recency": 0.87,
                "breadth": 0.92,
                "frequencyRationale": "Parser precedence regressions caught in 4 contributor sessions",
                "recencyRationale": "Referenced during May 2026 parser refactor review",
                "breadthRationale": "Affects parser, REPL, and plugin expression evaluation",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-05-03T18:40:00Z",
                    "provenance": "logs/nushell_contrib_20260503.jsonl:142",
                    "actor": "pipeline",
                },
                {
                    "action": "scored",
                    "timestamp": "2026-05-03T18:41:00Z",
                    "provenance": "logs/nushell_contrib_20260503.jsonl:142",
                    "actor": "pipeline",
                },
            ],
        },
        {
            "id": "cand_7",
            "title": "flatten Column Rename on Parent Conflicts",
            "content": "When building multi-stage table pipelines with `flatten`, later parent columns can collide with earlier names. Nushell renames conflicting columns automatically — always inspect output column names after each flatten stage and add explicit `rename` steps when downstream commands assume stable headers.",
            "state": "proposed",
            "confidence": 0.84,
            "provenance": "logs/nushell_contrib_20260613.jsonl:89",
            "createdAt": "2026-06-13T11:20:00Z",
            "confidenceBreakdown": {
                "frequency": 0.80,
                "recency": 0.86,
                "breadth": 0.83,
                "frequencyRationale": "Flatten rename collisions seen in 6 data-pipeline debugging sessions",
                "recencyRationale": "Last observed 2026-06-13 during table pipeline QA",
                "breadthRationale": "Applies to CLI data transforms and plugin table outputs",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-13T11:20:00Z",
                    "provenance": "logs/nushell_contrib_20260613.jsonl:89",
                    "actor": "pipeline",
                },
                {
                    "action": "scored",
                    "timestamp": "2026-06-13T11:21:00Z",
                    "provenance": "logs/nushell_contrib_20260613.jsonl:89",
                    "actor": "pipeline",
                },
            ],
        },
        {
            "id": "cand_8",
            "title": "Interrupt-Safe from json Streaming",
            "content": "For commands like `open large.json | from json` or stdin-fed parsers, verify Ctrl+C interrupts the reader without leaving a zombie task. When implementing or testing streaming parsers, use large inputs and confirm the REPL remains responsive after interrupt.",
            "state": "suggested",
            "confidence": 0.79,
            "provenance": "logs/nushell_contrib_20260614.jsonl:301",
            "createdAt": "2026-06-14T09:55:00Z",
            "confidenceBreakdown": {
                "frequency": 0.76,
                "recency": 0.82,
                "breadth": 0.78,
                "frequencyRationale": "Interrupt handling verified in 5 streaming parser test sessions",
                "recencyRationale": "Reproduced 2026-06-14 with large JSON fixture",
                "breadthRationale": "Covers from json, stdin readers, and plugin streaming paths",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-14T09:55:00Z",
                    "provenance": "logs/nushell_contrib_20260614.jsonl:301",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_suggested",
                    "timestamp": "2026-06-14T15:00:00Z",
                    "provenance": "logs/nushell_contrib_20260614.jsonl:301",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_9",
            "evalCaseId": "quirky_config_load_order",
            "evalCaseNamespace": "quirky",
            "title": "experimental_options Before Config Load",
            "content": "Early-boot experimental flags in nushell are read from the `experimental_options` environment variable before `config.nu` is evaluated. Set this variable in the parent shell or launcher script prior to starting `nu`, not inside config, when testing features that must be active at startup.",
            "state": "proposed",
            "confidence": 0.72,
            "provenance": "logs/nushell_contrib_20260611.jsonl:56",
            "createdAt": "2026-06-11T16:10:00Z",
            "scope": "nushell/config",
            "category": "api_behavior",
            "contradiction_ids": ["cand_16"],
            "confidenceBreakdown": {
                "frequency": 0.68,
                "recency": 0.74,
                "breadth": 0.71,
                "frequencyRationale": "Boot-order confusion surfaced in 4 experimental-flag debugging sessions",
                "recencyRationale": "Observed 2026-06-11 during config load order repro",
                "breadthRationale": "Affects shell startup, plugins, and CI launcher scripts",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-11T16:10:00Z",
                    "provenance": "logs/nushell_contrib_20260611.jsonl:56",
                    "actor": "pipeline",
                },
                {
                    "action": "contradiction_detected",
                    "timestamp": "2026-06-11T16:12:00Z",
                    "provenance": "logs/nushell_contrib_20260611.jsonl:56",
                    "actor": "pipeline",
                    "note": "Rival lesson cand_16: Experimental Flags in config.nu",
                },
            ],
        },
        {
            "id": "cand_10",
            "title": "ls Hidden Directory Glob Display",
            "content": "When fixing or testing `ls` with globs like `.*`, hidden directory names should retain their leading dot in output. Regression tests should cover both `ls .hidden` and pattern-based listings so dot-prefixed entries are not stripped or normalized away.",
            "state": "suggested",
            "confidence": 0.88,
            "provenance": "logs/nushell_contrib_20260511.jsonl:178",
            "createdAt": "2026-05-11T13:25:00Z",
            "confidenceBreakdown": {
                "frequency": 0.85,
                "recency": 0.84,
                "breadth": 0.90,
                "frequencyRationale": "Hidden-path display bugs caught in 5 ls regression sessions",
                "recencyRationale": "Still referenced in May 2026 filesystem command reviews",
                "breadthRationale": "Covers ls, glob, and path display formatting",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-05-11T13:25:00Z",
                    "provenance": "logs/nushell_contrib_20260511.jsonl:178",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_suggested",
                    "timestamp": "2026-05-12T09:00:00Z",
                    "provenance": "logs/nushell_contrib_20260511.jsonl:178",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_11",
            "title": "any/all Row Conditions vs Closures",
            "content": "Prefer row conditions over closures for simple predicates in `any` and `all` — they are easier to read and type-check. Reserve closures for multi-step logic; mixing both styles in one pipeline makes failures harder to localize during review.",
            "state": "suggested",
            "confidence": 0.86,
            "provenance": "logs/nushell_contrib_20260605.jsonl:224",
            "createdAt": "2026-06-05T10:45:00Z",
            "confidenceBreakdown": {
                "frequency": 0.83,
                "recency": 0.88,
                "breadth": 0.84,
                "frequencyRationale": "Row-condition preference noted in 6 pipeline review sessions",
                "recencyRationale": "Cited 2026-06-05 during any/all refactor",
                "breadthRationale": "Applies to table filters, plugins, and teaching examples",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-05T10:45:00Z",
                    "provenance": "logs/nushell_contrib_20260605.jsonl:224",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_suggested",
                    "timestamp": "2026-06-06T11:00:00Z",
                    "provenance": "logs/nushell_contrib_20260605.jsonl:224",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_12",
            "title": "Non-Blocking Reedline Completions",
            "content": "REPL completions must not block the main input thread. When touching Reedline completion paths, add timeout guards and verify the prompt stays interactive under slow filesystem or network-backed path providers.",
            "state": "decayed",
            "confidence": 0.68,
            "provenance": "logs/nushell_contrib_20260603.jsonl:412",
            "createdAt": "2026-06-03T08:30:00Z",
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-03T08:30:00Z",
                    "provenance": "logs/nushell_contrib_20260603.jsonl:412",
                    "actor": "pipeline",
                },
                {
                    "action": "decayed",
                    "timestamp": "2026-06-17T12:00:00Z",
                    "provenance": "logs/nushell_contrib_20260603.jsonl:412",
                    "actor": "pipeline",
                    "note": "Superseded by upstream Reedline timeout defaults in 0.98+",
                },
            ],
        },
        {
            "id": "cand_13",
            "title": "Decouple Plugin Protocol Version",
            "content": "Plugin protocol version should not track nushell semver one-to-one. Bump protocol only when the wire format changes, and document compatibility so patch releases do not break existing plugins that only need bugfix-level shell updates.",
            "state": "active",
            "confidence": 0.94,
            "provenance": "logs/nushell_contrib_20260420.jsonl:67",
            "createdAt": "2026-04-20T14:00:00Z",
            "confidenceBreakdown": {
                "frequency": 0.93,
                "recency": 0.91,
                "breadth": 0.95,
                "frequencyRationale": "Protocol semver guidance stable across 8 plugin maintainer sessions",
                "recencyRationale": "Still enforced in April–June 2026 release planning",
                "breadthRationale": "Affects plugin SDK, CI compatibility matrix, and release notes",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-04-20T14:00:00Z",
                    "provenance": "logs/nushell_contrib_20260420.jsonl:67",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_active",
                    "timestamp": "2026-04-21T10:00:00Z",
                    "provenance": "logs/nushell_contrib_20260420.jsonl:67",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_14",
            "title": "Guard stdout/stderr on Parent Exit",
            "content": "Child processes spawned by the shell must not write to stdout or stderr after the parent has closed those descriptors — doing so can trigger SIGABRT on some platforms. Audit spawn/exit paths and suppress or redirect IO during teardown.",
            "state": "suggested",
            "confidence": 0.81,
            "provenance": "logs/nushell_contrib_20260617.jsonl:193",
            "createdAt": "2026-06-17T07:15:00Z",
            "confidenceBreakdown": {
                "frequency": 0.78,
                "recency": 0.85,
                "breadth": 0.79,
                "frequencyRationale": "SIGABRT teardown bugs found in 4 spawn/exit debugging sessions",
                "recencyRationale": "Reproduced 2026-06-17 on macOS CI runner",
                "breadthRationale": "Covers external commands, plugins, and job control",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-17T07:15:00Z",
                    "provenance": "logs/nushell_contrib_20260617.jsonl:193",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_suggested",
                    "timestamp": "2026-06-17T12:00:00Z",
                    "provenance": "logs/nushell_contrib_20260617.jsonl:193",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_15",
            "title": "enforce-runtime-annotations Opt-Out Change",
            "content": "`enforce-runtime-annotations` is now opt-out rather than opt-in. When upgrading nushell or drafting release notes, call out this breaking behavior and verify plugins or scripts that relied on the old default still pass CI with annotations enabled.",
            "state": "active",
            "confidence": 0.90,
            "provenance": "logs/nushell_contrib_20260606.jsonl:118",
            "createdAt": "2026-06-06T12:50:00Z",
            "confidenceBreakdown": {
                "frequency": 0.88,
                "recency": 0.92,
                "breadth": 0.89,
                "frequencyRationale": "Breaking-change callouts required in 5 release-note reviews",
                "recencyRationale": "Flagged 2026-06-06 during 0.98 upgrade notes",
                "breadthRationale": "Affects plugins, scripts, and contributor onboarding docs",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-06T12:50:00Z",
                    "provenance": "logs/nushell_contrib_20260606.jsonl:118",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_active",
                    "timestamp": "2026-06-07T09:00:00Z",
                    "provenance": "logs/nushell_contrib_20260606.jsonl:118",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_16",
            "evalCaseId": "quirky_config_load_order",
            "evalCaseNamespace": "quirky",
            "evalCaseRole": "rival",
            "title": "Experimental Flags in config.nu",
            "content": "For most experimental nushell features, enable flags under the `$env.config` experimental section in `config.nu` so they persist across sessions. This is the preferred path for day-to-day development when flags do not need to be set before the first config parse.",
            "state": "proposed",
            "confidence": 0.77,
            "provenance": "logs/nushell_contrib_20260610.jsonl:44",
            "createdAt": "2026-06-10T09:05:00Z",
            "scope": "nushell/config",
            "category": "api_behavior",
            "contradiction_ids": ["cand_9"],
            "confidenceBreakdown": {
                "frequency": 0.74,
                "recency": 0.80,
                "breadth": 0.76,
                "frequencyRationale": "Config-based flag setup preferred in 5 day-to-day dev sessions",
                "recencyRationale": "Observed 2026-06-10 during experimental feature onboarding",
                "breadthRationale": "Applies to local dev and contributor config templates",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-10T09:05:00Z",
                    "provenance": "logs/nushell_contrib_20260610.jsonl:44",
                    "actor": "pipeline",
                },
                {
                    "action": "contradiction_detected",
                    "timestamp": "2026-06-10T09:07:00Z",
                    "provenance": "logs/nushell_contrib_20260610.jsonl:44",
                    "actor": "pipeline",
                    "note": "Rival lesson cand_9: experimental_options Before Config Load",
                },
            ],
        },
        {
            "id": "cand_17",
            "title": "Symlink Directory Completion Trailing Slash",
            "content": "Path completions for symlinked directories should normalize trailing slashes consistently between tab-complete and manual entry. Add tests for both `link/` and `link` forms so Reedline and the filesystem layer agree on the displayed path.",
            "state": "active",
            "confidence": 0.83,
            "provenance": "logs/nushell_contrib_20260428.jsonl:256",
            "createdAt": "2026-04-28T15:35:00Z",
            "confidenceBreakdown": {
                "frequency": 0.80,
                "recency": 0.82,
                "breadth": 0.86,
                "frequencyRationale": "Trailing-slash normalization bugs in 4 completion test sessions",
                "recencyRationale": "Regression tests added April 2026; still referenced in reviews",
                "breadthRationale": "Covers Reedline, path providers, and symlink edge cases",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-04-28T15:35:00Z",
                    "provenance": "logs/nushell_contrib_20260428.jsonl:256",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_active",
                    "timestamp": "2026-04-29T10:00:00Z",
                    "provenance": "logs/nushell_contrib_20260428.jsonl:256",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_18",
            "evalCaseId": "pathlib_preference",
            "evalCaseNamespace": "monica",
            "title": "Prefer pathlib Over os.path",
            "content": "Stop using os.path for new code — use pathlib.Path; it's the project standard.",
            "state": "proposed",
            "confidence": 0.81,
            "provenance": "logs/session_20260616.jsonl:201",
            "createdAt": "2026-06-16T17:45:00Z",
            "scope": "backend/python",
            "category": "constraint",
            "confidenceBreakdown": {
                "frequency": 0.79,
                "recency": 0.86,
                "breadth": 0.77,
                "frequencyRationale": "pathlib preference enforced in 7 Python refactor sessions",
                "recencyRationale": "Last observed 2026-06-16 during config_utils scaffolding",
                "breadthRationale": "Applies to eval harness, pipeline, and dashboard tooling",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-16T17:45:00Z",
                    "provenance": "logs/session_20260616.jsonl:201",
                    "actor": "pipeline",
                },
                {
                    "action": "scored",
                    "timestamp": "2026-06-16T17:46:00Z",
                    "provenance": "logs/session_20260616.jsonl:201",
                    "actor": "pipeline",
                },
            ],
        },
        {
            "id": "cand_19",
            "evalCaseId": "poison_negative_control_good",
            "evalCaseNamespace": "monica",
            "title": "Docstring and Test Policy Before Merge",
            "content": "Every public function needs a docstring and a unit test before merge.",
            "state": "suggested",
            "confidence": 0.89,
            "provenance": "logs/session_poison_demo.jsonl:14",
            "createdAt": "2026-06-17T11:20:00Z",
            "scope": "backend/python",
            "category": "constraint",
            "contradiction_ids": ["cand_20"],
            "confidenceBreakdown": {
                "frequency": 0.87,
                "recency": 0.91,
                "breadth": 0.84,
                "frequencyRationale": "Docstring + test policy cited in 8 calculator fixture reviews",
                "recencyRationale": "Validated 2026-06-17 during poison-control good arm",
                "breadthRationale": "Applies to eval harness fixtures and contributor onboarding",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-17T11:20:00Z",
                    "provenance": "logs/session_poison_demo.jsonl:14",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_suggested",
                    "timestamp": "2026-06-17T14:00:00Z",
                    "provenance": "logs/session_poison_demo.jsonl:14",
                    "actor": "human-gate",
                },
            ],
        },
        {
            "id": "cand_20",
            "evalCaseId": "poison_negative_control_bad",
            "evalCaseNamespace": "monica",
            "evalCaseRole": "rival",
            "title": "Never Add Docstrings",
            "content": "Never add docstrings; they bloat the codebase.",
            "state": "proposed",
            "confidence": 0.41,
            "provenance": "logs/session_poison_demo.jsonl:22",
            "createdAt": "2026-06-17T11:22:00Z",
            "scope": "backend/python",
            "category": "constraint",
            "contradiction_ids": ["cand_19"],
            "confidenceBreakdown": {
                "frequency": 0.38,
                "recency": 0.45,
                "breadth": 0.40,
                "frequencyRationale": "Single-session poison line — not corroborated elsewhere",
                "recencyRationale": "Flagged 2026-06-17 as conflicting with team docstring policy",
                "breadthRationale": "Contradicts established merge policy on calculator fixture",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-17T11:22:00Z",
                    "provenance": "logs/session_poison_demo.jsonl:22",
                    "actor": "pipeline",
                },
                {
                    "action": "contradiction_detected",
                    "timestamp": "2026-06-17T11:24:00Z",
                    "provenance": "logs/session_poison_demo.jsonl:22",
                    "actor": "pipeline",
                    "note": "Rival lesson cand_19: Docstring and Test Policy Before Merge",
                },
            ],
        },
        {
            "id": "cand_21",
            "evalCaseId": "promote_then_rerun",
            "evalCaseNamespace": "monica",
            "title": "Post-Promote Boot Order Lesson",
            "content": (
                "Early-boot experimental flags in nushell are read from the "
                "`experimental_options` environment variable before `config.nu` is evaluated. "
                "Set this variable in the parent shell or launcher script prior to starting "
                "`nu`, not inside config, when testing features that must be active at startup."
            ),
            "state": "active",
            "confidence": 0.91,
            "provenance": "logs/nushell_contrib_20260611.jsonl:56",
            "createdAt": "2026-06-11T16:10:00Z",
            "scope": "nushell/config",
            "category": "api_behavior",
            "confidenceBreakdown": {
                "frequency": 0.88,
                "recency": 0.93,
                "breadth": 0.89,
                "frequencyRationale": "Human-promoted after cand_9 resolution — ready for agent rerun",
                "recencyRationale": "Promoted to active 2026-06-18 for promote→graph demo",
                "breadthRationale": "Exercises human gate → KG → get context eval path",
            },
            "auditTrail": [
                {
                    "action": "distilled",
                    "timestamp": "2026-06-11T16:10:00Z",
                    "provenance": "logs/nushell_contrib_20260611.jsonl:56",
                    "actor": "pipeline",
                },
                {
                    "action": "promoted_to_suggested",
                    "timestamp": "2026-06-18T10:00:00Z",
                    "provenance": "logs/nushell_contrib_20260611.jsonl:56",
                    "actor": "human-gate",
                },
                {
                    "action": "promoted_to_active",
                    "timestamp": "2026-06-18T10:05:00Z",
                    "provenance": "logs/nushell_contrib_20260611.jsonl:56",
                    "actor": "human-gate",
                    "note": "Post-promote fact written to KG for promote_then_rerun eval",
                },
            ],
        },
    ]


def get_mock_candidate_dicts() -> list[dict]:
    """Demo narrative rows plus auto-generated rows for every registered eval case."""
    from eval_mock_bridge import (
        HAND_CRAFTED_EVAL_CASE_IDS,
        generate_eval_candidate_dicts,
    )

    return get_demo_candidate_dicts() + generate_eval_candidate_dicts(
        HAND_CRAFTED_EVAL_CASE_IDS
    )


def get_mock_graph_dict() -> dict:
    """Build a graph snapshot aligned with mock candidates for React graph view fixtures."""
    candidates = get_mock_candidate_dicts()
    nodes: list[dict] = []
    for row in candidates:
        node: dict = {
            "id": row["id"],
            "label": row["title"],
            "state": row["state"],
            "confidence": row["confidence"],
            "provenance": row["provenance"],
        }
        if row.get("scope"):
            node["scope"] = row["scope"]
        if row.get("category"):
            node["category"] = row["category"]
        nodes.append(node)

    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in candidates:
        for rival_id in row.get("contradiction_ids", []):
            pair = tuple(sorted([row["id"], rival_id]))
            key = (pair[0], pair[1], "contradiction")
            if key in seen:
                continue
            seen.add(key)
            edges.append({"src": row["id"], "dst": rival_id, "kind": "contradiction"})

    support_pairs = [
        ("cand_1", "cand_2"),
        ("cand_2", "cand_5"),
        ("cand_3", "cand_4"),
    ]
    for src_id, dst_id in support_pairs:
        pair = tuple(sorted([src_id, dst_id]))
        key = (pair[0], pair[1], "support")
        if key in seen:
            continue
        seen.add(key)
        edges.append({"src": src_id, "dst": dst_id, "kind": "support"})

    nushell_ids = [row["id"] for row in candidates if row["id"].startswith("cand_") and int(row["id"].split("_")[1]) >= 6 and int(row["id"].split("_")[1]) <= 17]

    scope_groups = [
        {"id": "frontend", "label": "Frontend", "parentId": None, "memberIds": []},
        {
            "id": "react",
            "label": "React",
            "parentId": "frontend",
            "memberIds": ["cand_2", "cand_5"],
        },
        {
            "id": "typescript",
            "label": "TypeScript",
            "parentId": "frontend",
            "memberIds": ["cand_1"],
        },
        {"id": "infra", "label": "Infrastructure", "parentId": None, "memberIds": []},
        {
            "id": "ci",
            "label": "CI / DevOps",
            "parentId": "infra",
            "memberIds": ["cand_3"],
        },
        {"id": "backend", "label": "Backend", "parentId": None, "memberIds": []},
        {
            "id": "python",
            "label": "Python",
            "parentId": "backend",
            "memberIds": ["cand_4", "cand_18", "cand_19", "cand_20"],
        },
        {
            "id": "nushell",
            "label": "Nushell",
            "parentId": None,
            "memberIds": nushell_ids,
        },
    ]

    eval_members: dict[str, list[str]] = {}
    for row in candidates:
        eval_case_id = row.get("evalCaseId")
        if not eval_case_id:
            continue
        ns = row.get("evalCaseNamespace") or "eval"
        eval_members.setdefault(ns, []).append(row["id"])

    for ns in sorted(eval_members):
        scope_groups.append(
            {
                "id": f"eval_{ns}",
                "label": f"Eval — {ns.title()}",
                "parentId": None,
                "memberIds": sorted(eval_members[ns]),
            }
        )

    return {"nodes": nodes, "edges": edges, "scopeGroups": scope_groups}
