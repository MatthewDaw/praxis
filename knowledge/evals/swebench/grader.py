"""Grade a SWE-rebench instance with the official swebench harness, arm64-adapted.

This is U2 of the PR-knowledge eval pilot. The official swebench harness *can*
build sympy 1.12–1.14 natively on arm64, but it mis-grades SWE-rebench instances
out of the box for two reasons, both load-bearing here:

1. **arch.** ``make_test_spec`` defaults to ``arch="x86_64"``; on this Windows-ARM
   host (via WSL2 Docker) that silently builds x86 images that won't run. The fix
   (smoke #4) is to rebind ``make_test_spec`` to force ``arch="arm64"`` in *all
   three* import sites — ``test_spec``, ``docker_build``, and ``run_evaluation`` —
   because the base-image build and the instance build take different code paths,
   and patching one alone still builds x86 for the other.

2. **install_config.** The official spec map runs sympy's native ``bin/test`` and
   parses it with ``parse_log_sympy``. SWE-rebench instances are graded with
   ``pytest -rA`` (their ``install_config.test_cmd``) whose output ``parse_log_sympy``
   can't read — every passing test is mis-recorded as failed. The fix is to inject
   the instance's own ``test_cmd`` into ``MAP_REPO_VERSION_TO_SPECS`` and repoint
   ``MAP_REPO_TO_PARSER["sympy/sympy"]`` at ``parse_log_pytest`` (the parser
   ``pydata/xarray`` already uses), mutating both the ``log_parsers`` and ``grading``
   copies of the map.

All adaptation is **runtime monkeypatch** in this module — nothing under the
installed/vendored swebench is edited (an explicit constraint).

The pure, offline-testable pieces (``prepare`` map mutation, ``write_predictions``,
``parse_report``) are split from the Docker-shelling piece (``grade``): swebench is
imported lazily inside the functions so this module imports cleanly even where
swebench isn't installed (the host), and the live Docker grade — reached through
WSL — is exercised by the smoke driver, never by unit tests. The Docker call is
behind the injected ``run_evaluation`` seam (``_run_evaluation``) so tests never
reach it.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # avoid importing U1 (and its deps) at module import for the host
    from knowledge.evals.swebench.instances import Instance

# The swebench arch token for the host: arm64 on this Apple/Windows-ARM machine,
# x86_64 elsewhere. Computed once; passed into the rebound make_test_spec default.
ARCH = "arm64" if platform.machine() in ("aarch64", "arm64") else "x86_64"


@dataclass
class GradeResult:
    """Outcome of grading one instance's patch against its FAIL_TO_PASS/PASS_TO_PASS."""

    resolved: bool
    fail_to_pass: dict[str, str]  # test id -> "PASSED" / "FAILED"
    pass_to_pass: dict[str, str]
    empty_patch: bool
    error: str | None = None


def prepare(instance: "Instance", *, arch: str = ARCH) -> None:
    """Apply the arch monkeypatch + inject the instance's ``install_config`` in place.

    Mutates the in-memory swebench maps so a subsequent ``run_evaluation.main`` builds
    arm64 images and grades with the instance's pytest ``test_cmd`` + ``parse_log_pytest``.
    Pure side effect on the loaded swebench module state — no Docker, no network — so the
    offline test asserts directly on the mutated maps. swebench is imported lazily here so
    the module imports on the host (where swebench isn't installed).
    """
    import swebench.harness.docker_build as db_mod
    import swebench.harness.grading as grading
    import swebench.harness.run_evaluation as re_mod
    import swebench.harness.test_spec.test_spec as ts_mod
    from swebench.harness import log_parsers as lp
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

    _patch_arch(ts_mod, db_mod, re_mod, arch)

    repo = instance.repo or "sympy/sympy"

    # (1) parser repoint: grab parse_log_pytest via a repo known to use it, then set it on
    #     sympy in *both* the log_parsers and grading copies of the shared map.
    pytest_parser = lp.MAP_REPO_TO_PARSER["pydata/xarray"]
    lp.MAP_REPO_TO_PARSER[repo] = pytest_parser
    grading.MAP_REPO_TO_PARSER[repo] = pytest_parser

    # (2) test_cmd override: swap the official spec's test_cmd for the instance's own
    #     pytest -rA command so the run emits PASSED/FAILED lines parse_log_pytest reads.
    test_cmd = instance.install_config.get("test_cmd")
    if test_cmd:
        MAP_REPO_VERSION_TO_SPECS[repo][instance.version]["test_cmd"] = test_cmd


def _patch_arch(ts_mod, db_mod, re_mod, arch: str) -> None:
    """Rebind ``make_test_spec`` to force ``arch`` across all three import sites.

    The base-image build (``docker_build``) and the instance build (``run_evaluation``)
    each captured their own reference to ``make_test_spec`` at import time, so a single
    patch leaves one path building the wrong arch. We wrap the original and default
    ``arch`` to the host value in every site that holds a reference.
    """
    orig = ts_mod.make_test_spec

    def _patched(
        instance,
        namespace=None,
        base_image_tag="latest",
        env_image_tag="latest",
        instance_image_tag="latest",
        arch=arch,
    ):
        return orig(instance, namespace, base_image_tag, env_image_tag, instance_image_tag, arch)

    ts_mod.make_test_spec = _patched
    db_mod.make_test_spec = _patched
    re_mod.make_test_spec = _patched


def write_predictions(instance_id: str, patch: str, path: str | Path) -> None:
    """Write the swebench predictions file (one row) with **LF** line endings.

    An empty/whitespace patch still writes a valid row (``model_patch`` = the patch as
    given); the harness then grades it as an empty patch → not resolved, without raising.
    LF is enforced explicitly because the patch may have been assembled on Windows and a
    CRLF in ``model_patch`` corrupts the diff the harness applies in the Linux container.
    """
    row = [{
        "instance_id": instance_id,
        "model_name_or_path": "praxis_eval",
        "model_patch": patch,
    }]
    text = json.dumps(row, ensure_ascii=False)
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def parse_report(report_json: dict, instance: "Instance") -> GradeResult:
    """Pure: read the per-instance ``report.json`` the harness writes → ``GradeResult``.

    The harness writes ``{<instance_id>: {"resolved": ..., "tests_status": {...}}}`` where
    each of FAIL_TO_PASS / PASS_TO_PASS holds ``{"success": [...], "failure": [...]}``.
    We resolve to ``True`` iff every FAIL_TO_PASS and PASS_TO_PASS test for the instance is
    in its ``success`` set (mirroring the harness's own resolved rule), and surface a
    per-test PASSED/FAILED map for case studies.
    """
    inst_report = report_json.get(instance.instance_id, report_json)
    status = inst_report.get("tests_status", {})

    f2p = _status_map(status.get("FAIL_TO_PASS", {}), instance.fail_to_pass)
    p2p = _status_map(status.get("PASS_TO_PASS", {}), instance.pass_to_pass)

    if "resolved" in inst_report:
        resolved = bool(inst_report["resolved"])
    else:
        resolved = bool(f2p or p2p) and all(
            v == "PASSED" for v in {**f2p, **p2p}.values()
        )

    return GradeResult(
        resolved=resolved,
        fail_to_pass=f2p,
        pass_to_pass=p2p,
        empty_patch=bool(inst_report.get("patch_is_None", False)),
    )


def _status_map(group: dict, expected: list[str]) -> dict[str, str]:
    """Build a {test_id: "PASSED"/"FAILED"} map from a harness ``success``/``failure`` group.

    Keyed on the instance's expected tests so a test the harness never reported (e.g. it
    errored before running) shows up as FAILED rather than silently dropping.
    """
    success = set(group.get("success", []))
    failure = set(group.get("failure", []))
    ids = list(expected) or sorted(success | failure)
    out: dict[str, str] = {}
    for tid in ids:
        out[tid] = "PASSED" if tid in success else "FAILED"
    return out


# The Docker-shelling seam: the live harness entrypoint. Injected so tests never reach
# Docker/WSL; ``grade`` calls it after prepare + write_predictions.
def _run_evaluation(**kwargs) -> None:
    """Invoke the official harness (Docker, Linux/WSL-only). Imported lazily."""
    import swebench.harness.run_evaluation as re_mod

    re_mod.main(**kwargs)


def _use_wsl(backend: str) -> bool:
    """Whether to bridge grading into WSL. ``auto`` picks WSL on any non-Linux host.

    The swebench harness is Linux/Docker-only (``import resource``), but the agent +
    ``run.py`` run on the Windows host. ``auto`` => grade in-process when already on Linux,
    else shell out to WSL (the validated path on this Windows-ARM box).
    """
    if backend == "wsl":
        return True
    if backend == "inprocess":
        return False
    return platform.system() != "Linux"  # "auto"


def grade(
    instance: "Instance",
    patch: str,
    *,
    run_id: str = "praxis_eval",
    dataset_name: str = "nebius/SWE-rebench",
    split: str = "test",
    timeout: int = 1800,
    predictions_path: str | Path | None = None,
    backend: str = "auto",
    run_evaluation: Callable[..., None] = _run_evaluation,
    wsl_grade: Callable[..., dict | None] | None = None,
) -> GradeResult:
    """Grade ``patch`` for ``instance`` end to end via the arm64-adapted swebench harness.

    Orchestrates: write the predictions file → produce the per-instance ``report.json``
    (in-process on Linux, or bridged into WSL on a Windows/macOS host — see ``backend``)
    → parse it. An empty/whitespace patch short-circuits to ``resolved=False`` without
    shelling out.

    ``backend`` is ``"auto"`` (WSL off-Linux, in-process on Linux), ``"wsl"``, or
    ``"inprocess"``. The in-process ``run_evaluation`` and the ``wsl_grade`` bridge are
    both injectable seams so unit tests never reach Docker/WSL.
    """
    empty = not patch.strip()

    pred_path = Path(predictions_path) if predictions_path else Path(f"preds_{run_id}.json")
    write_predictions(instance.instance_id, patch, pred_path)

    if empty:
        return GradeResult(resolved=False, fail_to_pass={}, pass_to_pass={}, empty_patch=True)

    if _use_wsl(backend):
        report = (wsl_grade or _wsl_grade)(
            instance, pred_path, run_id=run_id, dataset_name=dataset_name,
            split=split, timeout=timeout,
        )
    else:
        prepare(instance)
        run_evaluation(
            dataset_name=dataset_name,
            split=split,
            instance_ids=[instance.instance_id],
            predictions_path=str(pred_path),
            max_workers=1,
            force_rebuild=False,
            cache_level="env",
            clean=False,
            open_file_limit=4096,
            run_id=run_id,
            timeout=timeout,
            namespace=None,
            rewrite_reports=False,
            modal=False,
        )
        report = _locate_report(instance.instance_id, run_id)

    if not report:
        return GradeResult(
            resolved=False, fail_to_pass={}, pass_to_pass={}, empty_patch=False,
            error="report.json not found after grading",
        )
    return parse_report(report, instance)


def _locate_report(instance_id: str, run_id: str) -> dict | None:
    """Find + load the per-instance ``report.json`` the harness writes under its run tree.

    The harness writes to ``logs/run_evaluation/<run_id>/<model>/<instance_id>/report.json``;
    the model dir is not known here, so we glob for the instance's report under the run.
    """
    base = Path("logs") / "run_evaluation" / run_id
    matches = sorted(base.glob(f"**/{instance_id}/report.json"))
    if not matches:
        return None
    return json.loads(matches[-1].read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# WSL bridge: grade from a Windows host by shelling into WSL where swebench lives.
# --------------------------------------------------------------------------- #
# This Windows-ARM box runs the agent on the host but the grader only in WSL. These
# point at the lean WSL swebench venv + a writable WSL workdir for the harness's logs/;
# override via env for a different machine.
_WSL_PYTHON = os.environ.get("PRAXIS_WSL_PYTHON", "$HOME/swebench-smoke/.venv/bin/python")
_WSL_WORKDIR = os.environ.get("PRAXIS_WSL_WORKDIR", "$HOME/swebench-smoke")


def _win_to_wsl(path: str | Path) -> str:
    """Translate a Windows path (``C:\\x\\y``) to its WSL mount form (``/mnt/c/x/y``)."""
    p = str(path).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return f"/mnt/{p[0].lower()}{p[2:]}"
    return p


def _wsl_grade(
    instance: "Instance",
    pred_path: str | Path,
    *,
    run_id: str,
    dataset_name: str,
    split: str,
    timeout: int,
) -> dict | None:
    """Bridge grading into WSL: write meta, run the standalone worker, read the report back.

    The worker (``_wsl_grade_worker.py``) imports ONLY swebench, so it runs in the lean WSL
    venv that can't import the ``knowledge`` package. We pass the predictions file + a small
    metadata JSON (repo/version/test_cmd/instance/dataset) as ``/mnt/c`` paths and read the
    per-instance ``report.json`` back from a host temp file the worker writes into.
    """
    import subprocess
    import tempfile

    worker = Path(__file__).resolve().parent / "_wsl_grade_worker.py"
    meta = {
        "repo": instance.repo or "sympy/sympy",
        "version": instance.version,
        "test_cmd": instance.install_config.get("test_cmd", ""),
        "instance_id": instance.instance_id,
        "dataset_name": dataset_name,
        "split": split,
    }
    tmp = Path(tempfile.mkdtemp(prefix="praxis-wslgrade-"))
    meta_path = tmp / "meta.json"
    out_path = tmp / "report.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    cmd = (
        f"cd {_WSL_WORKDIR} && {_WSL_PYTHON} "
        f'"{_win_to_wsl(worker)}" "{_win_to_wsl(Path(pred_path).resolve())}" '
        f'"{_win_to_wsl(meta_path)}" {run_id} "{_win_to_wsl(out_path)}" {timeout}'
    )
    subprocess.run(["wsl", "-e", "bash", "-lc", cmd], check=True, timeout=timeout + 600)

    if not out_path.exists():
        return None
    return json.loads(out_path.read_text(encoding="utf-8")) or None
