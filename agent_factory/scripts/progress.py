"""Progress lines for any long-running step, so a supervisor can act before it finishes.

A HEARTBEAT PROVES LIVENESS, NOT PROGRESS. af-build's claim lease and a tmux session both tell
you a process exists; neither tells you how far along it is, whether it will finish this hour, or
whether the thing it is producing is getting worse. Those are the questions actually asked while a
job runs, and answering them by waiting for the job to end is the same as not answering them.

Measured on the first campaign to run an expensive arm: a graph model ran for 28 minutes emitting
nothing. Its per-seed scores were 0.6183 / 0.6273 / 0.4123 / 0.0491 -- it was diverging from the
third repeat onward, and the run was ALSO being truncated by the wall-clock budget. Both facts
existed inside the process the whole time. Both were only discoverable after it exited, by which
point the campaign had adjudicated a meaningless number and recorded it as a verdict against an
entire model family. Every question asked while it ran ("is it going well?") could only be
answered with CPU percentage, which was 394% right up to the end.

The contract is deliberately small: one line per unit of work, on stdout, prefixed so it can be
grepped out of a log a supervisor is tailing. No dependencies, no config, no log framework -- a
step that has to import something heavy to report progress will not report progress.

    p = Progress("M06 stgcn", total=20)          # 4 seeds x 5 folds
    for ...:
        p.step(score=fold_f1)
    p.done()

emits, one line per step:

    [progress] M06 stgcn 7/20 35% elapsed 9m48s eta 18m12s last=0.6183 mean=0.6221
    [progress] M06 stgcn 16/20 80% elapsed 22m01s eta 5m30s last=0.0491 mean=0.4267
    [progress][WARN] M06 stgcn: last=0.0491 is 3.2 sigma below the mean of the previous 15
"""

from __future__ import annotations

import sys
import time
from statistics import fmean, pstdev

#: Grep for this to pull progress out of an interleaved log.
PREFIX = "[progress]"
WARN_PREFIX = "[progress][WARN]"

#: How far below the running mean a value must fall before it is called out. A degrading run is
#: worth interrupting; noise is not. 3 sigma over at least this many prior samples keeps the
#: warning rare enough that it is still read when it fires.
DEGRADE_SIGMAS = 3.0
DEGRADE_MIN_SAMPLES = 4

#: Used when the prior samples have ZERO variance, where a sigma is undefined. A run that was
#: perfectly steady and then fell over is the clearest degradation there is, so it must still warn.
DEGRADE_RELATIVE_DROP = 0.10


def _hms(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


class Progress:
    """One line per unit of work, with an ETA and a degradation warning.

    ``total`` is what makes an ETA possible, and an ETA is what turns "it is still running" into a
    decision. Pass it whenever the unit count is known up front -- it usually is (folds x seeds,
    files to migrate, tickets in a set).
    """

    def __init__(self, label: str, total: int | None = None, *, stream=None,
                 min_interval_s: float = 0.0) -> None:
        self.label = label
        self.total = total
        self.stream = stream if stream is not None else sys.stdout
        self.min_interval_s = min_interval_s
        self.started = time.time()
        self.n = 0
        self.scores: list[float] = []
        self._last_emit = 0.0

    def _emit(self, line: str) -> None:
        print(line, file=self.stream, flush=True)   # flush: a buffered progress line is no line

    def step(self, score: float | None = None, note: str = "") -> None:
        """Record one completed unit. ``score`` enables the mean and the degradation warning."""
        self.n += 1
        elapsed = time.time() - self.started

        if score is not None:
            # Compare BEFORE appending, or a collapsing value pollutes the mean it is judged against.
            if len(self.scores) >= DEGRADE_MIN_SAMPLES:
                mu, sd = fmean(self.scores), pstdev(self.scores)
                if sd > 0:
                    if (mu - score) / sd >= DEGRADE_SIGMAS:
                        self._emit(f"{WARN_PREFIX} {self.label}: last={score:.4f} is "
                                   f"{(mu - score) / sd:.1f} sigma below the mean of the previous "
                                   f"{len(self.scores)} ({mu:.4f})")
                # Zero observed variance is the CLEAREST degradation signal, not a reason to stay
                # quiet: with sd == 0 any real drop is infinitely many sigma, and a `sd > 0` guard
                # silently skips exactly the case a reader would most want flagged -- a run that
                # was rock steady and then fell over. Fall back to a relative drop so a run that
                # is merely noise-free at 4 decimal places does not trip it.
                elif mu and (mu - score) / abs(mu) >= DEGRADE_RELATIVE_DROP:
                    self._emit(f"{WARN_PREFIX} {self.label}: last={score:.4f} dropped "
                               f"{100 * (mu - score) / abs(mu):.0f}% below the previous "
                               f"{len(self.scores)} ({mu:.4f}), which had no variance at all")
            self.scores.append(score)

        now = time.time()
        if self.min_interval_s and (now - self._last_emit) < self.min_interval_s \
                and (self.total is None or self.n < self.total):
            return
        self._last_emit = now

        parts = [PREFIX, self.label]
        if self.total:
            pct = 100.0 * self.n / self.total
            eta = (elapsed / self.n) * (self.total - self.n) if self.n else 0.0
            parts.append(f"{self.n}/{self.total} {pct:.0f}%")
            parts.append(f"elapsed {_hms(elapsed)} eta {_hms(eta)}")
        else:
            parts.append(f"{self.n} done elapsed {_hms(elapsed)}")
        if self.scores:
            parts.append(f"last={self.scores[-1]:.4f} mean={fmean(self.scores):.4f}")
        if note:
            parts.append(note)
        self._emit(" ".join(parts))

    def done(self, note: str = "") -> None:
        elapsed = time.time() - self.started
        tail = f" mean={fmean(self.scores):.4f}" if self.scores else ""
        self._emit(f"{PREFIX} {self.label} COMPLETE {self.n} unit(s) in {_hms(elapsed)}{tail}"
                   + (f" {note}" if note else ""))

def stream_progress(proc, echo=None, prefix: str = PREFIX) -> str:
    """Consume a subprocess's stdout LIVE, echoing progress lines, and return everything.

    The producer half of this module is useless without a correct consumer, and the obvious
    consumer is wrong in a way that looks right:

        for line in proc.stdout:      # WRONG -- hidden read-ahead buffer
            ...

    Iterating a pipe in text mode fills an internal read-ahead buffer before yielding, so lines
    are withheld until that buffer fills or the process exits. Measured: an arm emitted its first
    progress line at 1m31s and then nothing for 30 minutes, while the child sat at 373% CPU
    genuinely working. The producer was correct, `PYTHONUNBUFFERED` was set on the child, and the
    lines still did not arrive -- the supervisor's own reader was holding them.

    `iter(readline, "")` reads one line at a time with no read-ahead, which is the only form that
    actually streams.

    Also drains stdout continuously, which matters independently: a child that fills a pipe
    buffer nobody is reading BLOCKS. Collecting output only after `wait()` deadlocks any process
    chatty enough to fill 64KB.

    Returns the complete stdout so a caller can still parse a trailing summary from it.
    """
    echo = echo or (lambda line: print(line, flush=True))
    captured: list[str] = []
    if proc.stdout is None:
        return ""
    for line in iter(proc.stdout.readline, ""):
        captured.append(line)
        if line.startswith(prefix):
            echo(line.rstrip())
    return "".join(captured)
