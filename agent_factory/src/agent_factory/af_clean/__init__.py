"""af-clean: portable aggressive AI-slop cleanup.

This package grows one ticket at a time. R1 (``toolchain``) lands first: per-invocation
toolchain detection so nothing about the target repository's languages, package managers,
test runners, linters, or type checkers is ever hardcoded.

R12 (``findings``) defines the located-finding vocabulary every later detector/applier reuses: a
finding carries a ``file:line`` instance or it is discarded, a judgment-tier finding must
enumerate its cognitive-load chunks, every finding declares which pole of the signed slop axis
(bloat or fragmentation) it sits at, a DRY finding must carry an observable discriminator
(co-change or parameter-accretion) or it is dropped, an inline proposal against a helper with 3+
live callers is refused, and a consolidation that would need a flag or branch per caller is
rejected as a failed centralization rather than applied.

``exemptions`` (R3) lands the automatic exemption-manifest derivation: which paths af-clean must
never propose for deletion because they are generated, vendored, a lockfile, or a
language-convention immutable/fixture directory -- never a human-curated allowlist.

``reachability`` (R19) lands the pre-clean read-only measurement pass and the tri-state deletion
verdict: a code-derived call graph, per-symbol test coverage evidence gathered with zero edits,
the reachable/unreachable x covered/uncovered grid (keep / keep+test-debt / delete-with-binding /
quarantine), the bound-test-deletion guard, and staged excision to a fixed point across rounds.

R25 lands the blind-verification seam (B23/B46 in
``docs/brainstorms/2026-07-29-af-clean-requirements.md``): the Cleaner and the Verifier run in
separate contexts, and the Verifier subprocess is launched with a payload narrowed to just the
diff and the repo path.
"""
