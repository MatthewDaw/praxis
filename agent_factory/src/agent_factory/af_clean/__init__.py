"""af-clean: portable aggressive AI-slop cleanup.

This package grows one ticket at a time. ``exemptions`` (R3) lands the automatic
exemption-manifest derivation: which paths af-clean must never propose for deletion because
they are generated, vendored, a lockfile, or a language-convention immutable/fixture
directory -- never a human-curated allowlist.
"""
