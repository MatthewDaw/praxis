"""Durable rules for scoped and folded ML research arms.

The registry deliberately keeps this policy next to adjudication rather than leaving it
to lane prose: a scope changes what is being judged and a fold is only meaningful when
its constituent measurements are available in the same ledger.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def scope_from_params(params: object) -> dict[str, str] | None:
    if not isinstance(params, Mapping) or "scope" not in params:
        return None
    scope = params["scope"]
    if not isinstance(scope, Mapping) or set(scope) != {"group", "value"}:
        raise ValueError("arm scope must be exactly {'group', 'value'}")
    group, value = scope["group"], scope["value"]
    if not isinstance(group, str) or not group.strip() or not isinstance(value, str) or not value.strip():
        raise ValueError("arm scope group and value must be non-empty strings")
    return {"group": group.strip(), "value": value.strip()}


def declared_groups(spec: Mapping[str, object] | None) -> set[str]:
    if not isinstance(spec, Mapping):
        return set()
    entries: list[Mapping[str, object]] = []
    metric = spec.get("metric")
    if isinstance(metric, Mapping):
        entries.append(metric)
    metrics = spec.get("metrics")
    if isinstance(metrics, Sequence) and not isinstance(metrics, (str, bytes)):
        entries.extend(item for item in metrics if isinstance(item, Mapping))
    groups: set[str] = set()
    for entry in entries:
        raw = entry.get("groups")
        if isinstance(raw, Mapping):
            groups.update(str(key).strip() for key in raw if isinstance(key, str) and key.strip())
    return groups


def validate_scope(scope: dict[str, str] | None, spec: Mapping[str, object] | None) -> None:
    if scope is None:
        return
    groups = declared_groups(spec)
    if not groups:
        raise ValueError("scoped arm requires declared metric.groups in its CampaignSpec")
    if scope["group"] not in groups:
        raise ValueError(
            f"scoped arm names undeclared group {scope['group']!r}; declared groups are {sorted(groups)}"
        )


def constituent_run_ids(params: object) -> tuple[str, ...]:
    if not isinstance(params, Mapping) or "constituents" not in params:
        return ()
    raw = params["constituents"]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise ValueError("fold constituents must be a non-empty list of run ids")
    result = tuple(item.strip() if isinstance(item, str) else "" for item in raw)
    if not all(result) or len(set(result)) != len(result):
        raise ValueError("fold constituents must be distinct non-empty run ids")
    return result


def _parked_without_regression(registry: Any, run_id: str) -> bool:
    for event in reversed(registry.list_events()):
        if event.event_type != "run_adjudicated" or event.payload.get("run_id") != run_id:
            continue
        evidence = event.payload.get("adjudication_evidence")
        if not isinstance(evidence, Mapping):
            return True
        metrics = evidence.get("metrics")
        if isinstance(metrics, Mapping):
            return not any(isinstance(item, Mapping) and item.get("regressed") for item in metrics.values())
        return not bool(evidence.get("group_regressions"))
    return False


def validate_constituents(registry: Any, *, experiment_id: object, run_id: object, params: object) -> None:
    """Refuse an unmeasured / regressed fold before it can consume the GPU slot."""
    constituents = constituent_run_ids(params)
    if not constituents:
        return
    rows = {str(row["run_id"]): row for row in registry.rows("runs")}
    for constituent in constituents:
        if constituent == run_id:
            raise ValueError("fold cannot name itself as a constituent")
        row = rows.get(constituent)
        if row is None:
            raise ValueError(f"fold constituent {constituent!r} has not been measured")
        if row["experiment_id"] != experiment_id:
            raise ValueError(f"fold constituent {constituent!r} belongs to a different experiment")
        if row["status"] != "succeeded" or row["verdict"] not in {"adopted", "parked"}:
            raise ValueError(
                f"fold constituent {constituent!r} must be individually measured and adopted or non-regressed parked"
            )
        if row["verdict"] == "parked" and not _parked_without_regression(registry, constituent):
            raise ValueError(f"fold constituent {constituent!r} is parked with a regression")


def scoped_interval(
    paired_evidence: Mapping[str, object], *, scope: dict[str, str], policy: Mapping[str, object],
    run_id: str, champion_run_id: str, direction: str,
):
    """Bootstrap exactly the selected group's pairs; the target interval decides a scoped arm."""
    units = paired_evidence.get("units")
    if not isinstance(units, Sequence) or isinstance(units, (str, bytes)):
        raise ValueError("scoped arm requires paired evidence units")
    selected = [dict(unit) for unit in units if isinstance(unit, Mapping) and unit.get("stratum") == scope["value"]]
    if len(selected) < 2:
        raise ValueError(
            f"scoped arm target {scope['group']}={scope['value']!r} has fewer than two paired units"
        )
    import statistics
    candidate = statistics.fmean(float(unit["candidate"]) for unit in selected)
    champion = statistics.fmean(float(unit["champion"]) for unit in selected)
    from .paired_adjudication import paired_interval
    evidence = dict(paired_evidence)
    evidence["units"] = selected
    interval = paired_interval(policy, evidence, run_id=run_id, champion_run_id=champion_run_id,
                               direction=direction, candidate_metric=candidate,
                               champion_metric=champion)
    return interval
