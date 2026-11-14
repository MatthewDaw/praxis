import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    'ticket_test_scope', Path(__file__).parents[1] / 'scripts/af_ticket_test_scope.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_full_api_census_is_rejected_but_selected_route_tests_run():
    prefix = ['test-harness.ts', '--', 'npm', 'run', 'test:integration', '--workspace', 'apps/api', '--']
    assert module.rejection_reason(prefix)
    assert not module.rejection_reason(prefix + ['src/organizationOfficials.itest.ts'])


def test_import_graph_expansion_is_rejected():
    assert module.rejection_reason(['vitest', 'related', '--run', 'src/App.tsx'])
    assert module.rejection_reason(['vitest', 'run', '--changed=HEAD'])


def test_explicit_web_cases_and_league_directory_run():
    assert not module.rejection_reason(['vitest', 'run', 'src/screens/Standings.test.tsx'])
    assert not module.rejection_reason(['npm', 'test', '--', 'src/features/league/screens/D-scores/'])


def test_generic_browser_and_legacy_parity_are_rejected():
    assert module.rejection_reason(['npm', 'run', 'test:e2e'])
    assert module.rejection_reason(['npm', 'run', 'test:parity'])
    assert not module.rejection_reason(['npm', 'run', 'test:e2e', '--', 'tests/e2e/scores.spec.ts'])


def test_build_quality_and_dedicated_acceptance_are_unaffected():
    for command in [['npm', 'run', 'build'], ['tsc', '--noEmit'], ['npm', 'run', 'test:r021']]:
        assert not module.rejection_reason(command)


def test_dedicated_browser_configuration_can_define_selected_journey():
    assert not module.rejection_reason(['playwright', 'test', '--config', 'playwright.personal-forms.config.js'])
    assert module.rejection_reason(['playwright', 'test', '--config', 'playwright.config.js'])
