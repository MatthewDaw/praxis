from __future__ import annotations

import csv
from dataclasses import dataclass
from enum import Enum
import io
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from ._validation import ContractError, integer, number, text


LEDGER_V2_HEADER = (
    "commit", "metric_value", "memory_gb", "status", "description", "throughput",
    "diff_lines",
)
FAIR_LEDGER_STATUSES: frozenset[str] = frozenset({"ok", ""})


class LedgerStatus(str, Enum):
    OK = "ok"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ABORTED = "aborted"
    ERRORED = "errored"


class LedgerValidity(str, Enum):
    VALID = "valid"
    INVALID = "invalid"


class ThroughputUnit(str, Enum):
    ROWS_PER_SECOND = "rows_per_second"
    SAMPLES_PER_SECOND = "samples_per_second"
    SEQUENCES_PER_SECOND = "sequences_per_second"


@dataclass(frozen=True)
class LedgerRowV2:
    commit: str
    metric_value: float
    memory_gb: float
    status: LedgerStatus | str
    description: str
    throughput: float
    diff_lines: int
    validity: LedgerValidity | str | None = None
    throughput_units: ThroughputUnit | str | None = None

    def __post_init__(self) -> None:
        commit = text(self.commit, "ledger commit")
        metric = number(self.metric_value, "ledger metric_value")
        memory = number(self.memory_gb, "ledger memory_gb", minimum=0)
        throughput = number(self.throughput, "ledger throughput", minimum=0)
        diff_lines = integer(self.diff_lines, "ledger diff_lines")
        description = text(self.description, "ledger description")
        status = _enum(LedgerStatus, self.status, "ledger status")
        units = (None if self.throughput_units is None
                 else _enum(ThroughputUnit, self.throughput_units, "ledger throughput_units"))

        validity = self.validity
        validity = (None if validity is None
                    else _enum(LedgerValidity, validity, "ledger validity"))
        if validity is LedgerValidity.VALID and status is not LedgerStatus.OK:
            raise ContractError(
                f"ledger status {status.value!r} cannot be declared valid"
            )

        object.__setattr__(self, "commit", commit)
        object.__setattr__(self, "metric_value", metric)
        object.__setattr__(self, "memory_gb", memory)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "throughput", throughput)
        object.__setattr__(self, "diff_lines", diff_lines)
        object.__setattr__(self, "validity", validity)
        object.__setattr__(self, "throughput_units", units)

    @property
    def is_fair(self) -> bool:
        return self.status is LedgerStatus.OK and self.validity is LedgerValidity.VALID

    @classmethod
    def from_fields(cls, fields: list[str], *, line: int) -> "LedgerRowV2":
        if len(fields) != len(LEDGER_V2_HEADER):
            raise ContractError(
                f"ledger line {line} has {len(fields)} columns; expected {len(LEDGER_V2_HEADER)}"
            )
        try:
            return cls(
                commit=fields[0], metric_value=_numeric(fields[1], line, "metric_value"),
                memory_gb=_numeric(fields[2], line, "memory_gb"), status=fields[3],
                description=fields[4], throughput=_numeric(fields[5], line, "throughput"),
                diff_lines=_integer(fields[6], line, "diff_lines"),
            )
        except ContractError as exc:
            raise ContractError(f"ledger line {line}: {exc}") from exc

    def fields(self) -> tuple[str, ...]:
        return (
            self.commit, str(self.metric_value), str(self.memory_gb), self.status.value,
            self.description, str(self.throughput), str(self.diff_lines),
        )


@dataclass(frozen=True, order=True)
class LedgerRowIdentity:
    """Stable identity for one occurrence of a possibly repeated ledger join key."""

    commit: str
    occurrence: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "commit", text(self.commit, "ledger row identity commit"))
        integer(self.occurrence, "ledger row identity occurrence")


@dataclass(frozen=True)
class LedgerAnnotations:
    """Non-lossy semantic assertions kept beside, never invented inside, a legacy LedgerV2."""

    validity: Mapping[LedgerRowIdentity, LedgerValidity | str]
    throughput_units: Mapping[LedgerRowIdentity, ThroughputUnit | str]

    def __post_init__(self) -> None:
        if not isinstance(self.validity, Mapping) or not isinstance(self.throughput_units, Mapping):
            raise ContractError("ledger annotations must be mappings keyed by LedgerRowIdentity")
        if not all(isinstance(key, LedgerRowIdentity) for key in self.validity):
            raise ContractError("ledger validity annotations must be keyed by LedgerRowIdentity")
        if not all(isinstance(key, LedgerRowIdentity) for key in self.throughput_units):
            raise ContractError("ledger throughput annotations must be keyed by LedgerRowIdentity")
        object.__setattr__(self, "validity", MappingProxyType(dict(self.validity)))
        object.__setattr__(self, "throughput_units", MappingProxyType(dict(self.throughput_units)))


@dataclass(frozen=True)
class LedgerProjection:
    by_commit: Mapping[str, LedgerRowV2]
    fair_by_commit: Mapping[str, LedgerRowV2]
    unfair_by_commit: Mapping[str, tuple[LedgerRowV2, ...]]
    metric_values: Mapping[str, float]
    throughputs: Mapping[str, float]
    throughput_units: ThroughputUnit | None


@dataclass(frozen=True)
class LegacyLedgerMeasurement:
    """A scored row projected from either LedgerV2 or the historical ``val_bpb`` form."""

    commit: str
    metric_value: float
    throughput: float | None
    diff_lines: float | None
    status: str


@dataclass(frozen=True)
class LedgerCompatibilityProjection:
    """Behaviour-neutral views used while legacy ledgers migrate to strict LedgerV2.

    Parsing lives here so callers cannot drift on headers, blank/unscored rows, status
    fairness, or duplicate join keys.  This adapter deliberately does not weaken
    :meth:`LedgerV2.parse`: historical ``val_bpb`` and partial ledgers remain compatibility
    inputs, not new versions of the wire contract.
    """

    has_header: bool
    header: tuple[str, ...]
    raw_rows: tuple[Mapping[str, str], ...]
    commits: frozenset[str]
    metric_values: Mapping[str, float]
    duplicate_fair_metric_commits: tuple[str, ...]
    measurements: Mapping[str, LegacyLedgerMeasurement]
    duplicate_fair_commits: tuple[str, ...]


@dataclass(frozen=True)
class LedgerCompatibilityHeader:
    """Header-only view for callers whose validation must precede body consumption."""

    has_header: bool
    columns: tuple[str, ...]


def read_ledger_compatibility_header(path: Path) -> LedgerCompatibilityHeader:
    """Read only the first TSV record, preserving empty-file versus blank-header semantics."""
    with path.open(newline="") as stream:
        reader = csv.reader(stream, delimiter="\t")
        try:
            return LedgerCompatibilityHeader(
                True, tuple(column.strip() for column in next(reader)),
            )
        except StopIteration:
            return LedgerCompatibilityHeader(False, ())


def read_ledger_compatibility(path: Path) -> LedgerCompatibilityProjection:
    """Read one external TSV once and expose canonical legacy-compatible projections."""
    with path.open(newline="") as stream:
        reader = csv.reader(stream, delimiter="\t")
        try:
            header = tuple(column.strip() for column in next(reader))
        except StopIteration:
            return LedgerCompatibilityProjection(
                False, (), (), frozenset(), MappingProxyType({}), (), MappingProxyType({}), (),
            )

        raw_rows: list[Mapping[str, str]] = []
        commits: set[str] = set()
        metric_values: dict[str, float] = {}
        fair_metric_keys: set[str] = set()
        duplicate_metric_keys: set[str] = set()
        measurements: dict[str, LegacyLedgerMeasurement] = {}
        fair_keys: set[str] = set()
        duplicates: set[str] = set()
        commit_at = header.index("commit") if "commit" in header else None
        metric_at = next(
            (header.index(column) for column in ("metric_value", "val_bpb") if column in header),
            None,
        )
        throughput_at = header.index("throughput") if "throughput" in header else None
        diff_lines_at = header.index("diff_lines") if "diff_lines" in header else None
        status_at = header.index("status") if "status" in header else None

        for fields in reader:
            if not fields or not any(column.strip() for column in fields):
                continue
            raw_rows.append(MappingProxyType(dict(zip(header, fields))))
            # The historical commit-only reader intentionally uses column zero even when the
            # header is malformed.  Preserve that compatibility while named projections require
            # the canonical commit column below.
            if fields[0].strip():
                commits.add(fields[0].strip())
            # The value-only reader historically consumes columns zero and one while resolving
            # only ``status`` by name.  Keep that legacy projection explicit; the verdict view
            # below remains fully column-named.
            if len(fields) >= 2 and fields[0].strip():
                try:
                    positional_metric = float(fields[1])
                except ValueError:
                    pass
                else:
                    positional_commit = fields[0].strip()
                    positional_status = (
                        fields[status_at].strip()
                        if status_at is not None and len(fields) > status_at
                        else ""
                    )
                    if positional_status.lower() not in FAIR_LEDGER_STATUSES:
                        if positional_commit not in fair_metric_keys:
                            metric_values[positional_commit] = positional_metric
                    else:
                        if positional_commit in fair_metric_keys:
                            duplicate_metric_keys.add(positional_commit)
                        fair_metric_keys.add(positional_commit)
                        metric_values[positional_commit] = positional_metric
            if commit_at is None or metric_at is None:
                continue
            widest = max(commit_at, metric_at, throughput_at or 0, diff_lines_at or 0,
                         status_at or 0)
            if len(fields) <= widest or not fields[commit_at].strip():
                continue
            try:
                metric_value = float(fields[metric_at])
            except ValueError:
                continue
            try:
                throughput = (None if throughput_at is None else float(fields[throughput_at]))
                diff_lines = (None if diff_lines_at is None else float(fields[diff_lines_at]))
            except ValueError:
                # The verdict reader historically skipped this row before it could reserve a
                # fair join key.  A later fully measured retry therefore remains the first fair
                # row, rather than being misclassified as a duplicate.
                continue
            commit = fields[commit_at].strip()
            status = fields[status_at].strip() if status_at is not None else "ok"
            measurement = LegacyLedgerMeasurement(
                commit, metric_value, throughput, diff_lines, status,
            )
            if status.lower() not in FAIR_LEDGER_STATUSES:
                if commit not in fair_keys:
                    measurements[commit] = measurement
                continue
            if commit in fair_keys:
                duplicates.add(commit)
            fair_keys.add(commit)
            measurements[commit] = measurement

    return LedgerCompatibilityProjection(
        has_header=True,
        header=header,
        raw_rows=tuple(raw_rows),
        commits=frozenset(commits),
        metric_values=MappingProxyType(metric_values),
        duplicate_fair_metric_commits=tuple(sorted(duplicate_metric_keys)),
        measurements=MappingProxyType(measurements),
        duplicate_fair_commits=tuple(sorted(duplicates)),
    )


@dataclass(frozen=True)
class LedgerV2:
    rows: tuple[LedgerRowV2, ...]
    schema_version: int = 2

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ContractError(f"unsupported ledger schema_version {self.schema_version!r}")
        if not isinstance(self.rows, tuple) or not all(isinstance(row, LedgerRowV2) for row in self.rows):
            raise ContractError("ledger rows must be a tuple of LedgerRowV2 values")

    @classmethod
    def parse(cls, content: str) -> "LedgerV2":
        if not isinstance(content, str):
            raise ContractError("ledger content must be text")
        if "\x00" in content:
            raise ContractError("ledger contains a NUL byte")
        try:
            reader = csv.reader(io.StringIO(content, newline=""), delimiter="\t", strict=True)
            header = tuple(next(reader))
        except StopIteration as exc:
            raise ContractError("ledger is empty") from exc
        except csv.Error as exc:
            raise ContractError(f"ledger is malformed TSV: {exc}") from exc
        if header != LEDGER_V2_HEADER:
            raise ContractError(
                f"ledger header is not LedgerV2: expected {LEDGER_V2_HEADER!r}, got {header!r}"
            )
        rows: list[LedgerRowV2] = []
        try:
            for index, row_fields in enumerate(reader, 2):
                if not row_fields or (len(row_fields) == 1 and not row_fields[0]):
                    raise ContractError(f"ledger line {index} is blank")
                rows.append(LedgerRowV2.from_fields(row_fields, line=index))
        except csv.Error as exc:
            raise ContractError(f"ledger is malformed TSV: {exc}") from exc
        return cls(tuple(rows))

    @classmethod
    def from_rows(cls, rows: Iterable[LedgerRowV2]) -> "LedgerV2":
        return cls(tuple(rows))

    def project(self, annotations: LedgerAnnotations | None = None) -> LedgerProjection:
        fair: dict[str, LedgerRowV2] = {}
        unfair: dict[str, list[LedgerRowV2]] = {}
        units: ThroughputUnit | None = None
        occurrences: dict[str, int] = {}
        expected_identities: set[LedgerRowIdentity] = set()
        for row in self.rows:
            occurrence = occurrences.get(row.commit, 0)
            occurrences[row.commit] = occurrence + 1
            identity = LedgerRowIdentity(row.commit, occurrence)
            expected_identities.add(identity)
            raw_validity = (annotations.validity.get(identity) if annotations is not None
                            else row.validity)
            raw_units = (annotations.throughput_units.get(identity) if annotations is not None
                         else row.throughput_units)
            if raw_validity is None:
                if row.metric_value == 0 and row.status is LedgerStatus.OK:
                    raise ContractError(
                        f"ledger row {identity!r} zero-metric ok row requires explicit validity; "
                        "zero alone cannot distinguish a real measurement from a failed run"
                    )
                raw_validity = (LedgerValidity.VALID if row.status is LedgerStatus.OK
                                else LedgerValidity.INVALID)
            if raw_units is None:
                raise ContractError(
                    f"ledger row {identity!r} has unknown throughput units; supply typed LedgerAnnotations"
                )
            validity = _enum(LedgerValidity, raw_validity, "ledger validity")
            row_units = _enum(ThroughputUnit, raw_units, "ledger throughput_units")
            if validity is LedgerValidity.VALID and row.status is not LedgerStatus.OK:
                raise ContractError(
                    f"ledger status {row.status.value!r} cannot be declared valid"
                )
            if units is None:
                units = row_units
            elif row_units is not units:
                raise ContractError(
                    "ledger throughput units are incomparable: "
                    f"{units.value!r} and {row_units.value!r}"
                )
            if validity is LedgerValidity.VALID:
                if row.commit in fair:
                    raise ContractError(
                        f"ledger carries more than one fair row for join key {row.commit!r}; "
                        "use a unique '{sha}:{arm_tag}' key for each fair run"
                    )
                fair[row.commit] = row
            else:
                unfair.setdefault(row.commit, []).append(row)

        if annotations is not None:
            validity_keys = set(annotations.validity)
            unit_keys = set(annotations.throughput_units)
            if validity_keys != expected_identities or unit_keys != expected_identities:
                raise ContractError(
                    "ledger annotations must name every row identity exactly: "
                    f"expected {sorted(expected_identities)!r}, validity has {sorted(validity_keys)!r}, "
                    f"throughput_units has {sorted(unit_keys)!r}"
                )

        by_commit = {key: attempts[-1] for key, attempts in unfair.items()}
        by_commit.update(fair)
        return LedgerProjection(
            by_commit=MappingProxyType(by_commit), fair_by_commit=MappingProxyType(fair),
            unfair_by_commit=MappingProxyType({key: tuple(value) for key, value in unfair.items()}),
            metric_values=MappingProxyType({key: value.metric_value for key, value in fair.items()}),
            throughputs=MappingProxyType({key: value.throughput for key, value in fair.items()}),
            throughput_units=units,
        )

    def serialize(self) -> str:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(LEDGER_V2_HEADER)
        writer.writerows(row.fields() for row in self.rows)
        return stream.getvalue()


def _enum(enum_type: type[Enum], value: object, label: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        known = ", ".join(repr(member.value) for member in enum_type)
        raise ContractError(f"{label} must be one of {known}; got {value!r}") from exc


def _numeric(value: str, line: int, field: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ContractError(f"ledger line {line} {field} must be numeric") from exc


def _integer(value: str, line: int, field: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ContractError(f"ledger line {line} {field} must be an integer") from exc
