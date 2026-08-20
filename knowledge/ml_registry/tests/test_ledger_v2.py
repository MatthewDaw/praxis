from __future__ import annotations

import pytest

from knowledge.ml_registry.contracts import (
    LEDGER_V2_HEADER,
    LedgerAnnotations,
    LedgerRowIdentity,
    LedgerRowV2,
    LedgerStatus,
    LedgerV2,
    LedgerValidity,
    ThroughputUnit,
)
from knowledge.ml_registry.contracts._validation import ContractError


def row(commit: str = "abc:arm", **changes: object) -> LedgerRowV2:
    values = {
        "commit": commit, "metric_value": 0.8, "memory_gb": 1.5,
        "status": LedgerStatus.OK, "description": "measured arm", "throughput": 12.0,
        "diff_lines": 4,
    }
    values.update(changes)
    return LedgerRowV2(**values)


def annotations(*entries: tuple[str, int, LedgerValidity, ThroughputUnit]) -> LedgerAnnotations:
    validity = {LedgerRowIdentity(commit, occurrence): valid
                for commit, occurrence, valid, _ in entries}
    units = {LedgerRowIdentity(commit, occurrence): unit
             for commit, occurrence, _, unit in entries}
    return LedgerAnnotations(validity, units)


def test_exact_seven_column_header_and_byte_stable_round_trip() -> None:
    assert LEDGER_V2_HEADER == (
        "commit", "metric_value", "memory_gb", "status", "description", "throughput", "diff_lines",
    )
    ledger = LedgerV2.from_rows([row()])
    payload = ledger.serialize()
    assert payload.splitlines()[0] == "\t".join(LEDGER_V2_HEADER)
    assert LedgerV2.parse(payload) == ledger


@pytest.mark.parametrize("status", ["mystery", "", "failed"])
def test_unknown_or_blank_status_is_refused(status: str) -> None:
    with pytest.raises(ContractError, match="status must be one of"):
        row(status=status)


def test_raw_parse_keeps_wire_facts_unannotated_and_projection_requires_units() -> None:
    parsed = LedgerV2.parse(LedgerV2.from_rows([row()]).serialize())
    assert parsed.rows[0].validity is None
    assert parsed.rows[0].throughput_units is None
    with pytest.raises(ContractError, match="unknown throughput units"):
        parsed.project()


def test_zero_metric_requires_explicit_sidecar_but_zero_can_be_valid() -> None:
    ledger = LedgerV2.from_rows([row(metric_value=0)])
    with pytest.raises(ContractError, match="zero-metric ok row requires explicit validity"):
        ledger.project()
    projection = ledger.project(annotations(
        ("abc:arm", 0, LedgerValidity.VALID, ThroughputUnit.ROWS_PER_SECOND)
    ))
    assert projection.metric_values == {"abc:arm": 0}


@pytest.mark.parametrize("field", ["memory_gb", "throughput", "diff_lines"])
def test_numeric_measurements_are_nonnegative(field: str) -> None:
    with pytest.raises(ContractError, match=field):
        row(**{field: -1})


def test_metric_is_finite_but_its_domain_may_include_negative_values() -> None:
    assert row(metric_value=-0.25).metric_value == -0.25
    with pytest.raises(ContractError, match="finite number"):
        row(metric_value=float("nan"))


def test_row_identity_occurrence_is_nonnegative() -> None:
    with pytest.raises(ContractError, match="occurrence"):
        LedgerRowIdentity("sha:arm", -1)


def test_annotation_mappings_are_frozen_copies() -> None:
    identity = LedgerRowIdentity("sha:arm", 0)
    validity = {identity: LedgerValidity.VALID}
    units = {identity: ThroughputUnit.ROWS_PER_SECOND}
    sidecar = LedgerAnnotations(validity, units)
    validity.clear()
    units.clear()
    assert sidecar.validity == {identity: LedgerValidity.VALID}
    assert sidecar.throughput_units == {identity: ThroughputUnit.ROWS_PER_SECOND}
    with pytest.raises(TypeError):
        sidecar.validity[identity] = LedgerValidity.INVALID  # type: ignore[index]


def test_status_and_validity_must_agree_at_projection_boundary() -> None:
    ledger = LedgerV2.from_rows([row(status=LedgerStatus.BUDGET_EXHAUSTED)])
    with pytest.raises(ContractError, match="cannot be declared valid"):
        ledger.project(annotations(
            ("abc:arm", 0, LedgerValidity.VALID, ThroughputUnit.ROWS_PER_SECOND)
        ))


def test_external_validity_can_void_an_ok_row_without_rewriting_history() -> None:
    ledger = LedgerV2.from_rows([row(metric_value=0)])
    projection = ledger.project(annotations(
        ("abc:arm", 0, LedgerValidity.INVALID, ThroughputUnit.ROWS_PER_SECOND)
    ))
    assert "abc:arm" not in projection.fair_by_commit
    assert projection.unfair_by_commit["abc:arm"] == (ledger.rows[0],)


def test_throughput_units_are_typed_and_homogeneous_per_ledger() -> None:
    ledger = LedgerV2.from_rows([row("a"), row("b")])
    with pytest.raises(ContractError, match="throughput_units must be one of"):
        ledger.project(annotations(
            ("a", 0, LedgerValidity.VALID, "items_per_fortnight"),  # type: ignore[arg-type]
            ("b", 0, LedgerValidity.VALID, ThroughputUnit.ROWS_PER_SECOND),
        ))
    with pytest.raises(ContractError, match="incomparable"):
        ledger.project(annotations(
            ("a", 0, LedgerValidity.VALID, ThroughputUnit.ROWS_PER_SECOND),
            ("b", 0, LedgerValidity.VALID, ThroughputUnit.SAMPLES_PER_SECOND),
        ))


def test_unfair_attempt_can_be_rerun_fairly_under_same_join_key() -> None:
    unfair = row("sha:arm", status=LedgerStatus.BUDGET_EXHAUSTED)
    fair = row("sha:arm")
    projection = LedgerV2.from_rows([unfair, fair]).project(annotations(
        ("sha:arm", 0, LedgerValidity.INVALID, ThroughputUnit.ROWS_PER_SECOND),
        ("sha:arm", 1, LedgerValidity.VALID, ThroughputUnit.ROWS_PER_SECOND),
    ))
    assert projection.by_commit["sha:arm"] is fair
    assert projection.fair_by_commit["sha:arm"] is fair
    assert projection.unfair_by_commit["sha:arm"] == (unfair,)
    assert projection.metric_values == {"sha:arm": 0.8}
    assert projection.throughputs == {"sha:arm": 12.0}


def test_multiple_unfair_attempts_remain_diagnostic_and_latest_projects() -> None:
    first = row("sha:arm", status=LedgerStatus.ABORTED)
    second = row("sha:arm", status=LedgerStatus.ERRORED)
    projection = LedgerV2.from_rows([first, second]).project(annotations(
        ("sha:arm", 0, LedgerValidity.INVALID, ThroughputUnit.ROWS_PER_SECOND),
        ("sha:arm", 1, LedgerValidity.INVALID, ThroughputUnit.ROWS_PER_SECOND),
    ))
    assert projection.by_commit["sha:arm"] is second
    assert projection.unfair_by_commit["sha:arm"] == (first, second)
    assert "sha:arm" not in projection.fair_by_commit


def test_duplicate_fair_join_key_is_refused_regardless_of_unfair_attempts() -> None:
    ledger = LedgerV2.from_rows([
        row("sha:arm"), row("sha:arm", status=LedgerStatus.ABORTED), row("sha:arm"),
    ])
    with pytest.raises(ContractError, match="more than one fair row"):
        ledger.project(annotations(
            ("sha:arm", 0, LedgerValidity.VALID, ThroughputUnit.ROWS_PER_SECOND),
            ("sha:arm", 1, LedgerValidity.INVALID, ThroughputUnit.ROWS_PER_SECOND),
            ("sha:arm", 2, LedgerValidity.VALID, ThroughputUnit.ROWS_PER_SECOND),
        ))


def test_sidecar_must_cover_every_row_identity_exactly() -> None:
    ledger = LedgerV2.from_rows([row("a")])
    with pytest.raises(ContractError, match="must name every row identity exactly"):
        ledger.project(LedgerAnnotations(
            {LedgerRowIdentity("a", 0): LedgerValidity.VALID},
            {LedgerRowIdentity("a", 0): ThroughputUnit.ROWS_PER_SECOND,
             LedgerRowIdentity("extra", 0): ThroughputUnit.ROWS_PER_SECOND},
        ))


@pytest.mark.parametrize("payload, message", [
    ("", "empty"),
    ("commit\tmetric_value\n", "not LedgerV2"),
    ("\t".join(LEDGER_V2_HEADER) + "\n\n", "blank"),
    ("\t".join(LEDGER_V2_HEADER) + "\n" + "\t".join(["x"] * 8) + "\n", "columns"),
    ("\t".join(LEDGER_V2_HEADER) + "\n\x00", "NUL"),
])
def test_malformed_input_is_refused(payload: str, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        LedgerV2.parse(payload)
