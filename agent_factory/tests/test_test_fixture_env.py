import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'test_fixture_env', Path(__file__).parents[1] / 'scripts/af_test_fixture_env.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_matching_inherited_defaults_are_removed():
    original = {'ORIGIN': 'application-origin', 'PROXY': 'application-proxy', 'OTHER': 'retained'}
    cleaned, removed = module.without_inherited_defaults(original, original, ('ORIGIN', 'PROXY'))
    assert cleaned == {'OTHER': 'retained'}
    assert removed == ['ORIGIN', 'PROXY']
    assert original['ORIGIN'] == 'application-origin'


def test_explicit_fixture_overrides_are_preserved():
    original = {'ORIGIN': 'fixture-origin', 'PROXY': ''}
    cleaned, removed = module.without_inherited_defaults(
        original, {'ORIGIN': 'application-origin', 'PROXY': 'application-proxy'}, ('ORIGIN', 'PROXY')
    )
    assert cleaned == original
    assert removed == []


def test_absent_defaults_and_unlisted_keys_are_not_changed():
    original = {'ORIGIN': 'fixture-origin', 'OTHER': 'retained'}
    cleaned, removed = module.without_inherited_defaults(original, {}, ('ORIGIN',))
    assert cleaned == original
    assert removed == []
