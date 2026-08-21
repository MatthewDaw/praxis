from agent_factory.af_clean.findings import admit_finding
from agent_factory.af_clean.ml_registry_cli_dead_symbol import ALLOWLIST, BATCHES, WITNESSES, finding


def test_every_cli_symbol_batch_is_one_located_admitted_finding():
    assert set(BATCHES) == {
        "_checked_model_budgets", "_update_registered_model",
        "_refuse_a_campaign_with_no_floor", "_parse_intervention",
    }
    assert all(admit_finding(finding(batch)).admitted for batch in BATCHES.values())


def test_cli_symbol_batches_are_source_plus_reachability_test_with_two_witnesses():
    assert ALLOWLIST == {
        "knowledge/ml_registry/cli/registry.py",
        "knowledge/ml_registry/tests/test_cli_dead_reachability.py",
    }
    assert len(WITNESSES) == 2
