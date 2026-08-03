"""R39/B36: an axis re-guidance or anchor edit to a `promote_universal` seeded check (e.g.
``minimalism-dry``) must reach tickets that already froze the OLD rubric onto a pinned entry —
``_norm_validation`` freezes the rubric at synthesis time and ``frozen_rubric_for`` never re-reads
``seeded_checks.toml``, so without this migration an open ticket would keep grading against
stale axis guidance forever.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
from _fake_praxis import SanctionedWrites  # noqa: E402

PLAN = ("team-app", "prd-team-app")

OLD_RUBRIC = {"axes": [{"name": "minimalism", "threshold": 0.8, "guidance": "old"}]}
NEW_RUBRIC = {"axes": [{"name": "proportionality", "threshold": 0.8, "guidance": "new"}]}


def _entry(source_check_id, rubric):
    return {"validation_id": "v1", "covers": ["minimalism-dry"], "kind": "graded",
            "rubric": rubric, "source_check_id": source_check_id}


def test_pure_migration_patches_stale_frozen_entry():
    meta = {ts.M_PINNED_CHECKS: [_entry("minimalism-dry", OLD_RUBRIC)]}
    patch = ts.migrate_pinned_universal(meta, "minimalism-dry", NEW_RUBRIC)
    assert patch is not None
    assert patch[ts.M_PINNED_CHECKS][0]["rubric"] == NEW_RUBRIC


def test_pure_migration_is_a_noop_when_already_current():
    meta = {ts.M_PINNED_CHECKS: [_entry("minimalism-dry", NEW_RUBRIC)]}
    assert ts.migrate_pinned_universal(meta, "minimalism-dry", NEW_RUBRIC) is None


def test_pure_migration_ignores_other_checks_and_absent_entries():
    meta = {ts.M_PINNED_CHECKS: [_entry("other-check", OLD_RUBRIC)]}
    assert ts.migrate_pinned_universal(meta, "minimalism-dry", NEW_RUBRIC) is None
    assert ts.migrate_pinned_universal({}, "minimalism-dry", NEW_RUBRIC) is None


class FakePraxis(SanctionedWrites):
    def __init__(self):
        self.patches: dict[str, dict] = {}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self.patches[cid] = meta_dict
        return {"id": cid, "meta": meta_dict}


def test_driver_patches_only_stale_tickets(monkeypatch):
    fake = FakePraxis()
    monkeypatch.setattr(ts, "_praxis", fake)
    tickets = [
        {"id": "R1", "meta": {ts.M_PINNED_CHECKS: [_entry("minimalism-dry", OLD_RUBRIC)]}},
        {"id": "R2", "meta": {ts.M_PINNED_CHECKS: [_entry("minimalism-dry", NEW_RUBRIC)]}},  # current
        {"id": "R3", "meta": {ts.M_PINNED_CHECKS: []}},  # no universal entry at all
    ]
    migrated = ts.migrate_universal_pinned_entries(tickets, "minimalism-dry", NEW_RUBRIC, ref=PLAN)
    assert migrated == ["R1"]
    assert fake.patches["R1"][ts.M_PINNED_CHECKS][0]["rubric"] == NEW_RUBRIC
    assert "R2" not in fake.patches and "R3" not in fake.patches
