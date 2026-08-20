from __future__ import annotations

from dataclasses import dataclass
import csv
import io
from typing import Iterable

from ._validation import ContractError, integer, number, text


LEDGER_V2_HEADER = ("commit", "metric_value", "memory_gb", "status", "description", "throughput", "diff_lines")


@dataclass(frozen=True)
class LedgerRowV2:
    commit: str
    metric_value: float
    memory_gb: float
    status: str
    description: str
    throughput: float
    diff_lines: int

    @classmethod
    def from_fields(cls, fields: list[str], *, line: int) -> "LedgerRowV2":
        if len(fields) != len(LEDGER_V2_HEADER):
            raise ContractError(f"ledger line {line} has {len(fields)} columns; expected 7")
        return cls(text(fields[0], f"ledger line {line} commit"),
                   number(_numeric(fields[1], line, "metric_value"), f"ledger line {line} metric_value"),
                   number(_numeric(fields[2], line, "memory_gb"), f"ledger line {line} memory_gb", minimum=0),
                   fields[3], text(fields[4], f"ledger line {line} description"),
                   number(_numeric(fields[5], line, "throughput"), f"ledger line {line} throughput", minimum=0),
                   integer(_integer(fields[6], line, "diff_lines"), f"ledger line {line} diff_lines"))

    def fields(self) -> tuple[str, ...]:
        return (self.commit, str(self.metric_value), str(self.memory_gb), self.status,
                self.description, str(self.throughput), str(self.diff_lines))


@dataclass(frozen=True)
class LedgerV2:
    rows: tuple[LedgerRowV2, ...]
    schema_version: int = 2

    @classmethod
    def parse(cls, content: str) -> "LedgerV2":
        reader = csv.reader(io.StringIO(content), delimiter="\t")
        try:
            header = tuple(next(reader))
        except StopIteration as exc:
            raise ContractError("ledger is empty") from exc
        if header != LEDGER_V2_HEADER:
            raise ContractError(f"ledger header is not LedgerV2: expected {LEDGER_V2_HEADER!r}, got {header!r}")
        return cls(tuple(LedgerRowV2.from_fields(row, line=index) for index, row in enumerate(reader, 2)))

    @classmethod
    def from_rows(cls, rows: Iterable[LedgerRowV2]) -> "LedgerV2":
        return cls(tuple(rows))

    def serialize(self) -> str:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(LEDGER_V2_HEADER)
        writer.writerows(row.fields() for row in self.rows)
        return stream.getvalue()


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
