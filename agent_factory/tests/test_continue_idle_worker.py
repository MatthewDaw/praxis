import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'continue_idle', Path(__file__).parents[1] / 'scripts/af_continue_idle_worker.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_unfinished_ticket_conversations_continue():
    for state in ['finished', 'blocked', '']:
        assert not module.needs_continuation(state, 'claude', 'bypass permissions on')
    assert module.needs_continuation('incomplete', 'claude', 'Still to do: tests.\nbypass permissions on')
    assert module.needs_continuation('in_progress', 'codex', 'Ask Codex to do anything')


def test_working_or_queued_conversations_are_untouched():
    for busy in ['esc to interrupt', 'Waiting for background', 'Queued follow-up',
                 'queued message', 'ctrl+x ctrl+s to send now']:
        assert not module.needs_continuation('in_progress', 'claude', 'bypass permissions on\n' + busy)


def test_shell_and_unknown_readiness_are_not_treated_as_idle_workers():
    assert not module.needs_continuation('incomplete', 'bash', 'bypass permissions on')
    assert not module.needs_continuation('incomplete', 'claude', 'Starting...')


def test_background_shell_or_monitor_footer_is_still_working():
    for footer in ['bypass permissions on · 1 shell, 2 monitors', 'bypass permissions on · 1 monitor']:
        assert not module.needs_continuation('in_progress', 'claude', footer)
