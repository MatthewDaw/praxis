"""What compute does THIS box actually have, and can it satisfy a declared lane?

WHY THIS EXISTS. A campaign declares the compute it needs; nothing checked whether the machine
about to run it has that compute. The ml_registry preflight asks seven questions -- STATUS,
COMPLETE, LEDGER, SEED, CORPUS, DISPATCH, STRUCTURE -- and every one of them is about CORPORA and
BOOKKEEPING. None is about the hardware.

The failure that makes it worth a module: this box has no GPU at all (no driver, no /dev/nvidia*,
torch installed as the +cpu build). A GPU-declared campaign run here does not crash. Most ML code
falls back to CPU silently and by design, so the arm runs, produces numbers, and those numbers are
recorded against a campaign that claims to have measured a GPU regime. The result is not merely
slow -- it is a MEASUREMENT OF A DIFFERENT THING, reported as real, with nothing anywhere marking
it. A crash would have been the kinder outcome.

So the lane becomes a question about the host, answered before anything runs, with the evidence
attached to the answer.

DELIBERATELY CONSERVATIVE. ``gpu`` is satisfied only on POSITIVE evidence that a usable device is
present. "Cannot tell" answers NO -- an unprovable GPU is exactly the state that produces a silent
CPU fallback, so absence of proof and proof of absence get the same, safe treatment. That is the
opposite of the direction a capacity check is usually written in, and it is the whole point.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from glob import glob

#: The closed set of lanes, matching hooks/_ticket_state.py's ``_LANE_DEFAULTS`` and plan_gate's
#: R_DEVICE_CLOSED_SET. A lane outside it is a typo, and a typo must never silently mean "cpu".
LANES = ("cpu", "gpu")

_PROBE_TIMEOUT_S = int(os.environ.get("AF_HOST_PROBE_TIMEOUT_S", "20"))

#: Operator override, for a host whose GPU this module genuinely cannot see (an exotic runtime, a
#: passthrough device node this does not know about). Named loudly, and every refusal prints it, so
#: taking it is a decision someone made rather than a default nobody noticed.
_GPU_OVERRIDE_ENV = "AF_HOST_ASSUME_GPU"


@dataclass(frozen=True)
class Capability:
    """One lane's availability on this host, with the evidence that decided it."""

    lane: str
    available: bool
    evidence: list[str] = field(default_factory=list)

    def why(self) -> str:
        return "; ".join(self.evidence) or "no evidence gathered"


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, check=False, capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT_S)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def _gpu_capability() -> Capability:
    """Three independent probes, because each answers a different way of not having a GPU.

    A machine can have device nodes and no driver, a driver and no device, or a perfectly working
    device that ``nvidia-smi`` is not installed to talk to. Any ONE positive is enough; it takes
    all three coming back negative to refuse.
    """
    evidence: list[str] = []

    if os.environ.get(_GPU_OVERRIDE_ENV, "").strip() not in ("", "0"):
        return Capability("gpu", True, [f"{_GPU_OVERRIDE_ENV} set — operator asserts a GPU is present"])

    nodes = sorted(glob("/dev/nvidia*"))
    evidence.append(f"device nodes: {', '.join(nodes) if nodes else 'none (/dev/nvidia* absent)'}")

    if shutil.which("nvidia-smi"):
        code, out = _run(["nvidia-smi", "-L"])
        first = next((l for l in out.splitlines() if l.strip()), "")
        evidence.append(f"nvidia-smi -L: exit {code} — {first[:120] or 'no output'}")
        if code == 0 and "GPU" in out:
            return Capability("gpu", True, evidence)
    else:
        evidence.append("nvidia-smi: not installed")

    # Last, and last for a reason: importing torch is by far the most expensive probe, and on a
    # CPU-only box it is also the most conclusive -- the +cpu wheel cannot be made to see a device.
    # sys.executable, not a bare "python3": the interpreter that will RUN the arm is the one whose
    # torch build decides whether a device is reachable, and a bare python3 routinely resolves to a
    # different one with no torch at all -- which would report "not importable" as though that were
    # evidence about the hardware.
    code, out = _run([
        sys.executable or "python3", "-c",
        "import torch;print('CUDA', torch.cuda.is_available(), torch.cuda.device_count(), torch.__version__)",
    ])
    if code == 0:
        line = next((l for l in out.splitlines() if l.startswith("CUDA")), out.strip())
        evidence.append(f"torch: {line[:120]}")
        if "CUDA True" in out:
            return Capability("gpu", True, evidence)
    else:
        evidence.append(f"torch: not importable by {sys.executable or 'python3'}")

    return Capability("gpu", False, evidence)


def capability(lane: str) -> Capability:
    """Can this host satisfy ``lane``? Raises ``ValueError`` on a lane outside the closed set —
    a typo must never resolve to a lane that happens to be available."""
    lane_n = str(lane or "").strip().lower()
    if lane_n not in LANES:
        raise ValueError(f"unknown lane {lane!r} — must be one of {list(LANES)}")
    if lane_n == "cpu":
        # No core COUNT here, deliberately. tools/check_no_core_derived_cap.py fails the build on any
        # core-derived expression under agent_factory/, and it is right to: a number read off the host
        # reshapes behaviour per machine, which is exactly what the fixed R15 lanes exist to prevent.
        # It cannot tell "deriving a cap" from "printing evidence", and it should not have to — the
        # cpu lane is satisfiable on any host that got far enough to ask, and the count adds nothing.
        return Capability("cpu", True, ["this host is running the factory, so the cpu lane is satisfiable"])
    return _gpu_capability()


def refusal_message(lane: str, cap: Capability, *, subject: str) -> str:
    """The refusal an operator has to be able to act on without reading this file."""
    return (
        f"{subject} declares device={lane!r}, and this host cannot satisfy it.\n"
        f"  evidence : {cap.why()}\n"
        "  why this is a REFUSAL and not a warning: nothing here would crash. ML code falls back "
        "to CPU silently and by design, so the run produces numbers and those numbers get recorded "
        "as a measurement of a regime that was never exercised.\n"
        f"  options  : run it on a host that has the device; re-declare it as device=cpu if the "
        f"acceptance is fixture-based and never touches the device; or, if this host really does "
        f"have a GPU this could not see, set {_GPU_OVERRIDE_ENV}=1 (recorded in the evidence)."
    )


def main(argv: list[str] | None = None) -> int:
    """``python -m agent_factory.host_capability <lane>`` — exit 0 if satisfiable, 1 if not.

    Exists so a shell preflight can ask the question without reimplementing the probes; the whole
    class of bug here is a second, slightly different copy of "do we have a GPU".
    """
    import argparse

    ap = argparse.ArgumentParser(prog="agent_factory.host_capability")
    ap.add_argument("lane", help=f"one of {list(LANES)}")
    ap.add_argument("--subject", default="this unit of work",
                    help="what is being refused, for the message")
    ap.add_argument("--quiet", action="store_true", help="print only the one-line evidence")
    args = ap.parse_args(argv)

    try:
        cap = capability(args.lane)
    except ValueError as exc:
        print(str(exc))
        return 2
    if cap.available:
        print(f"{cap.lane}: available — {cap.why()}")
        return 0
    print(cap.why() if args.quiet else refusal_message(args.lane, cap, subject=args.subject))
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
