"""af-clean: the portable aggressive AI-slop cleanup engine.

This package currently holds the blind-verification seam (B23/B46 in
``docs/brainstorms/2026-07-29-af-clean-requirements.md``): the Cleaner and the
Verifier run in separate contexts, and the Verifier subprocess is launched with a
payload narrowed to just the diff and the repo path.
"""
