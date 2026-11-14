import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'hooks'))
import _ticket_state as ts


class GradeRecordingTests(unittest.TestCase):
    def record(self, kind, passed=True, source=None, verdict=None):
        meta = {'pinned_checks': [{'validation_id': 'review', 'kind': kind, 'passed': None}]}
        with patch.object(ts, '_meta', return_value=meta), patch.object(ts._praxis, 'write_build_state', return_value={}) as write:
            ts.record_validation_pass('ticket', 'review', passed, source=source, verdict=verdict)
            return write.call_args.args[1]['pinned_checks'][0]

    def test_worker_cannot_substitute_binary_check_for_grade(self):
        with self.assertRaisesRegex(ValueError, 'external judge'):
            self.record('graded')

    def test_external_judge_record_keeps_its_verdict(self):
        verdict = {'passed': True, 'code_hash': 'exact-reviewed-state'}
        result = self.record('graded', source='graded-judge', verdict=verdict)
        self.assertEqual(result['source'], 'graded-judge')
        self.assertEqual(result['verdict'], verdict)

    def test_binary_execution_and_honest_failed_grade_still_record(self):
        self.assertTrue(self.record('binary')['passed'])
        self.assertFalse(self.record('graded', passed=False)['passed'])

    def test_generated_exemption_source_is_preserved(self):
        self.assertEqual(self.record('graded', source='path-exemption')['source'], 'path-exemption')


if __name__ == '__main__':
    unittest.main()
