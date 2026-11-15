import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('sonnet_judge', Path(__file__).parents[1] / 'scripts/af-sonnet-judge.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class JudgeTests(unittest.TestCase):
    def test_prompt_is_delimited_from_variadic_configuration(self):
        command = m.judge_command('/bin/claude')
        self.assertEqual(command[-1], '--')
        self.assertEqual(command[command.index('--mcp-config') + 1], '{"mcpServers":{}}')

    def test_judge_has_no_tools_or_persisted_conversation(self):
        command = m.judge_command('/bin/claude')
        self.assertEqual(command[command.index('--tools') + 1], '')
        self.assertIn('--strict-mcp-config', command)
        self.assertIn('--no-session-persistence', command)
        self.assertEqual(command[command.index('--model') + 1], 'sonnet')


if __name__ == '__main__':
    unittest.main()
