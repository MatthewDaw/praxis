"""In-domain / out-of-domain observation-signal classification and the
attention-needed determination (the ``observability-signals`` check).

See ``docs/observation-signal-domains.md`` for the convention this module
implements: an IN_DOMAIN signal is hook-fired and forgeable by the build
session; an OUT_OF_DOMAIN signal is external and trustworthy. No
attention-needed / control / terminal-state decision may rest on an
IN_DOMAIN signal alone -- this module enforces that by construction:
:func:`attention_needed` never reads an IN_DOMAIN signal at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: The single configured source the attention-needed determination
#: consults for staleness. There is exactly one silence threshold, never a
#: per-caller override, so "stalled" means the same thing everywhere.
SILENCE_THRESHOLD_S = 900


class SignalDomain(str, Enum):
    IN_DOMAIN = "in_domain"  # hook-fired, forgeable by the build session
    OUT_OF_DOMAIN = "out_of_domain"  # external, trustworthy


@dataclass(frozen=True)
class ObservationSignal:
    domain: SignalDomain
    observed_at: float


def attention_needed(
    signals: list[ObservationSignal],
    *,
    now: float,
    silence_threshold_s: float = SILENCE_THRESHOLD_S,
) -> bool:
    """True iff the OUT_OF_DOMAIN signals are silent past
    ``silence_threshold_s`` (or absent entirely). IN_DOMAIN signals are
    never consulted -- a forged fresh one cannot mask real staleness, and a
    suppressed one cannot manufacture a false attention-needed result.
    """
    out_of_domain = [s for s in signals if s.domain is SignalDomain.OUT_OF_DOMAIN]
    if not out_of_domain:
        return True  # no trustworthy signal at all -> treat as needing attention
    latest = max(s.observed_at for s in out_of_domain)
    return (now - latest) > silence_threshold_s
