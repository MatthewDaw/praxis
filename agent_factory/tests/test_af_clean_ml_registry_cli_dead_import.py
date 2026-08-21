from agent_factory.af_clean.findings import admit_finding
from agent_factory.af_clean.ml_registry_cli_dead_import import ALLOWLIST, BATCHES, WITNESSES, finding


def test_each_import_batch_is_located_and_admitted():
    assert len(BATCHES) == 10
    assert all(admit_finding(finding(batch)).admitted for batch in BATCHES.values())


def test_import_batch_boundary_is_source_reachability_and_full_witnesses():
    assert len(ALLOWLIST) == 2
    assert len(WITNESSES) == 2
