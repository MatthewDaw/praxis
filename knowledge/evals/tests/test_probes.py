"""Probe harness: for every case dir shipping a probe.json, prove the
deterministic checks actually catch the behavior.

probe.json = {"good": <PASSES every check>, "bad": <FAILS at least one>}.
Cases without probe.json are skipped (batch-1 cases have none yet).
"""

import json

import pytest

from knowledge.evals.eval_def import Artifact, EvalContext
from knowledge.evals.run import iter_case_dirs, load_case, run_checks

_PROBE_DIRS = [d for d in iter_case_dirs() if (d / "probe.json").exists()]


def _probe_ctx(case, output: str) -> EvalContext:
    """An EvalContext for a probe string.

    When the case declares ``output_file``, the probe string *is* that file's
    content, so synthesize the matching created-artifact — that makes
    ``writes_file`` / ``output_file`` cases probe-able with plain good/bad strings.
    """
    artifacts = [Artifact(path=case.output_file, status="created")] if case.output_file else []
    return EvalContext(case_id=case.id, output=output, artifacts=artifacts)


@pytest.mark.parametrize("case_dir", _PROBE_DIRS, ids=lambda d: d.name)
def test_probe_good_passes_bad_fails(case_dir):
    case = load_case(case_dir)
    probe = json.loads((case_dir / "probe.json").read_text(encoding="utf-8"))

    good = run_checks(case, _probe_ctx(case, probe["good"]))
    assert good, "case must declare at least one deterministic check"
    assert all(c.passed for c in good), [
        (c.name, c.evidence) for c in good if not c.passed
    ]

    bad = run_checks(case, _probe_ctx(case, probe["bad"]))
    assert any(not c.passed for c in bad), "bad output must fail at least one check"
