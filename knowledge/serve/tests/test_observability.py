"""Acceptance test for the ``observability-signals`` check (af87a2da...):
every signal is classified in-domain or out-of-domain, the attention-needed
determination is reached from out-of-domain signals alone when in-domain
ones are forged or suppressed, and the silence threshold is the single
configured source consulted.
"""

from __future__ import annotations

from knowledge.serve.observability_signals import (
    SILENCE_THRESHOLD_S,
    ObservationSignal,
    SignalDomain,
    attention_needed,
)

NOW = 10_000.0


def test_fresh_out_of_domain_signal_means_no_attention_needed():
    signals = [ObservationSignal(SignalDomain.OUT_OF_DOMAIN, NOW - 10)]

    assert attention_needed(signals, now=NOW) is False


def test_stale_out_of_domain_signal_needs_attention_despite_a_forged_fresh_in_domain_one():
    signals = [
        ObservationSignal(SignalDomain.OUT_OF_DOMAIN, NOW - SILENCE_THRESHOLD_S - 1),
        ObservationSignal(SignalDomain.IN_DOMAIN, NOW),  # forged/fresh -- must not mask staleness
    ]

    assert attention_needed(signals, now=NOW) is True


def test_suppressed_in_domain_signal_does_not_manufacture_attention_needed():
    # No in-domain signal at all (suppressed): the fresh out-of-domain
    # signal alone still governs the determination.
    signals = [ObservationSignal(SignalDomain.OUT_OF_DOMAIN, NOW - 10)]

    assert attention_needed(signals, now=NOW) is False


def test_no_out_of_domain_signal_at_all_is_treated_as_needing_attention():
    signals = [ObservationSignal(SignalDomain.IN_DOMAIN, NOW)]

    assert attention_needed(signals, now=NOW) is True


def test_single_configured_silence_threshold_is_the_sole_source_consulted():
    at_threshold = [ObservationSignal(SignalDomain.OUT_OF_DOMAIN, NOW - SILENCE_THRESHOLD_S)]
    past_threshold = [ObservationSignal(SignalDomain.OUT_OF_DOMAIN, NOW - SILENCE_THRESHOLD_S - 0.01)]

    assert attention_needed(at_threshold, now=NOW) is False
    assert attention_needed(past_threshold, now=NOW) is True
