"""Progress lines: an ETA and a degradation warning while the job is still running.

The failure this exists for: a graph-model arm ran 28 minutes emitting nothing. Its per-seed
scores were 0.6183 / 0.6273 / 0.4123 / 0.0491 -- diverging from the third repeat onward -- and it
was simultaneously being truncated by a wall-clock budget. Both facts lived inside the process the
whole time and were only discoverable after it exited.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from progress import DEGRADE_MIN_SAMPLES, PREFIX, WARN_PREFIX, Progress  # noqa: E402


def _run(scores, total=None, **kw):
    buf = io.StringIO()
    p = Progress("arm", total=total, stream=buf, **kw)
    for s in scores:
        p.step(score=s)
    p.done()
    return buf.getvalue().splitlines()


def test_every_step_emits_a_greppable_line() -> None:
    lines = [line for line in _run([0.6, 0.61, 0.62]) if line.startswith(PREFIX)]
    assert len(lines) == 4                      # 3 steps + COMPLETE


def test_a_total_yields_percent_and_eta() -> None:
    """An ETA is what turns 'it is still running' into a decision."""
    line = _run([0.6, 0.61], total=10)[1]
    assert "2/10 20%" in line and "eta" in line


def test_without_a_total_it_still_reports_count_and_elapsed() -> None:
    line = _run([0.6])[0]
    assert "1 done" in line and "elapsed" in line


def test_the_real_M06_sequence_warns_before_the_run_ends() -> None:
    """The whole point. This is the observed sequence, padded to the sample minimum."""
    scores = [0.61, 0.62, 0.60, 0.63, 0.6183, 0.6273, 0.4123, 0.0491]
    out = "\n".join(_run(scores, total=20))
    assert WARN_PREFIX in out
    warn_at = [i for i, line in enumerate(_run(scores, total=20)) if line.startswith(WARN_PREFIX)]
    assert warn_at and warn_at[0] < len(scores)     # fired mid-run, not at the end


def test_a_steady_run_never_warns() -> None:
    """A warning that fires on noise is a warning nobody reads."""
    assert WARN_PREFIX not in "\n".join(_run([0.70, 0.69, 0.71, 0.70, 0.695, 0.705, 0.70]))


def test_no_warning_before_enough_samples() -> None:
    """Two points have no meaningful spread; calling a third a collapse would be superstition."""
    few = [0.7] * (DEGRADE_MIN_SAMPLES - 1) + [0.01]
    assert WARN_PREFIX not in "\n".join(_run(few))


def test_the_collapsing_value_does_not_pollute_the_mean_it_is_judged_against() -> None:
    """Compared BEFORE being appended, or a large drop inflates the SD and hides itself."""
    lines = _run([0.70, 0.70, 0.70, 0.70, 0.01])
    warn = [line for line in lines if line.startswith(WARN_PREFIX)]
    assert warn and "0.7000" in warn[0]          # mean of the PREVIOUS four


def test_min_interval_throttles_but_always_emits_the_last_step() -> None:
    """A chatty step should not flood a supervisor's log, but the final unit must be visible."""
    lines = _run([0.6] * 5, total=5, min_interval_s=3600)
    assert any("5/5 100%" in line for line in lines)


def test_done_reports_total_time_and_mean() -> None:
    last = _run([0.6, 0.8])[-1]
    assert "COMPLETE" in last and "2 unit(s)" in last and "mean=0.7000" in last


def test_stream_progress_yields_lines_as_they_arrive_not_in_a_batch() -> None:
    """The consumer half. `for line in proc.stdout` fills a hidden read-ahead buffer and withholds
    lines until it fills or the process exits -- measured as a first progress line at 1m31s
    followed by 30 minutes of silence from a child that was working the whole time."""
    import subprocess
    import sys as _sys

    from progress import stream_progress

    child = subprocess.Popen(
        [_sys.executable, "-u", "-c",
         "import sys\n"
         "for i in range(3):\n"
         "    print(f'[progress] unit {i}', flush=True)\n"
         "print('{\"done\": true}')\n"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    seen = []
    out = stream_progress(child, echo=seen.append)
    child.wait()

    assert len(seen) == 3                       # every progress line echoed
    assert '{"done": true}' in out              # and the summary still captured
    assert "[progress] unit 0" in out
