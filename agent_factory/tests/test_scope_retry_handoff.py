import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    'scope_retry_cleanup', Path(__file__).parents[1] / 'scripts/af-stop-owned-scopes.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RetryHandoffTests(unittest.TestCase):
    def test_repeated_cleanup_preserves_an_unconsumed_valid_handoff(self):
        fake = SimpleNamespace(handoff_path=lambda: Path('/private/handoff'),
                               read_valid=lambda _: {'cleanup_skipped': True},
                               skip_cleanup_once=Mock(return_value=False))
        with patch.dict('sys.modules', {'af_live_handoff': fake}), patch.object(module.subprocess, 'run') as run:
            self.assertEqual(module.main(), 0)
            self.assertEqual(module.main(), 0)
            run.assert_not_called()

    def test_expired_handoff_does_not_protect_unrelated_or_owned_scopes(self):
        fake = SimpleNamespace(handoff_path=lambda: Path('/expired/handoff'),
                               read_valid=lambda _: None, skip_cleanup_once=Mock())
        def run(command, **_):
            if 'list-units' in command:
                return SimpleNamespace(stdout='owned.scope loaded active\nother.scope loaded active\n')
            if 'show' in command:
                return SimpleNamespace(stdout='factory.service' if 'owned.scope' in command else 'other.service')
            return SimpleNamespace(stdout='')
        with patch.dict('sys.modules', {'af_live_handoff': fake}), patch.dict(module.os.environ, {'AF_SERVICE_UNIT': 'factory.service'}), patch.object(module.subprocess, 'run', side_effect=run) as calls:
            self.assertEqual(module.main(), 0)
            stops = [call.args[0] for call in calls.call_args_list if 'stop' in call.args[0]]
            self.assertEqual(stops, [['systemctl', '--user', 'stop', 'owned.scope']])


if __name__ == '__main__':
    unittest.main()
