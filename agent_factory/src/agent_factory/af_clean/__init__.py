"""af-clean: portable aggressive AI-slop cleanup.

This package grows one ticket at a time. R12 (``findings``) defines the located-finding
vocabulary every later detector/applier reuses: a finding carries a ``file:line`` instance or it
is discarded, a judgment-tier finding must enumerate its cognitive-load chunks, every finding
declares which pole of the signed slop axis (bloat or fragmentation) it sits at, a DRY finding
must carry an observable discriminator (co-change or parameter-accretion) or it is dropped, an
inline proposal against a helper with 3+ live callers is refused, and a consolidation that would
need a flag or branch per caller is rejected as a failed centralization rather than applied.
"""
