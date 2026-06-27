"""Standalone SWE-rebench grading worker — runs INSIDE WSL, where swebench + Docker live.

The agent (host ``claude``) and the grader (swebench, Linux/Docker-only via ``import
resource``) live in different OS environments on a Windows-ARM box. The patch is the
OS-agnostic bridge: the host writes a predictions file + a small metadata JSON, then
shells out to this worker (``wsl ... python _wsl_grade_worker.py ...``) which builds and
grades the instance in WSL and writes the per-instance ``report.json`` back to a
host-readable path.

CRITICAL: this file imports **only swebench + stdlib**, never the ``knowledge`` package —
the lean WSL swebench venv doesn't have praxis's deps (tracing/pydantic/etc.), so importing
``knowledge.*`` here would fail. It is therefore a sibling of ``grader.py`` but shares no
imports with it; the arch + install_config monkeypatch logic is duplicated on purpose
(productized from the validated smoke driver ``grade_rebench_arm.py``).

Usage (invoked by ``grader._wsl_grade``):
    python _wsl_grade_worker.py <predictions.json> <meta.json> <run_id> <out_report.json> <timeout>

``meta.json`` = ``{"repo", "version", "test_cmd", "instance_id", "dataset_name", "split"}``.
On success it writes the harness's per-instance ``report.json`` to ``<out_report.json>``;
on a missing report it writes ``{}``.
"""

import json
import platform
import sys
from pathlib import Path


def _apply_patches(repo: str, version: str, test_cmd: str) -> None:
    """Arch (arm64) monkeypatch across the three make_test_spec sites + install_config inject."""
    import swebench.harness.docker_build as db_mod
    import swebench.harness.grading as grading
    import swebench.harness.run_evaluation as re_mod
    import swebench.harness.test_spec.test_spec as ts_mod
    from swebench.harness import log_parsers as lp
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.test_spec import make_test_spec as _orig

    arch = "arm64" if platform.machine() in ("aarch64", "arm64") else "x86_64"

    def _patched(instance, namespace=None, base_image_tag="latest", env_image_tag="latest",
                 instance_image_tag="latest", arch=arch):
        return _orig(instance, namespace, base_image_tag, env_image_tag, instance_image_tag, arch)

    ts_mod.make_test_spec = db_mod.make_test_spec = re_mod.make_test_spec = _patched

    # SWE-rebench grades sympy with pytest, not sympy's native bin/test — repoint the parser
    # (grab parse_log_pytest via a repo that uses it) and inject the instance's own test_cmd.
    pytest_parser = lp.MAP_REPO_TO_PARSER["pydata/xarray"]
    lp.MAP_REPO_TO_PARSER[repo] = pytest_parser
    grading.MAP_REPO_TO_PARSER[repo] = pytest_parser
    if test_cmd:
        MAP_REPO_VERSION_TO_SPECS[repo][version]["test_cmd"] = test_cmd


def main(argv: list[str]) -> int:
    preds_path, meta_path, run_id, out_path, timeout = argv[1], argv[2], argv[3], argv[4], int(argv[5])
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))

    _apply_patches(meta["repo"], meta["version"], meta.get("test_cmd", ""))

    import swebench.harness.run_evaluation as re_mod

    re_mod.main(
        dataset_name=meta.get("dataset_name", "nebius/SWE-rebench"),
        split=meta.get("split", "test"),
        instance_ids=[meta["instance_id"]],
        predictions_path=preds_path,
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

    # The harness writes logs/run_evaluation/<run_id>/<model>/<instance_id>/report.json
    # relative to cwd; copy the parsed report out to the host-readable out_path.
    matches = sorted(Path("logs/run_evaluation", run_id).glob(f"**/{meta['instance_id']}/report.json"))
    report = json.loads(matches[-1].read_text(encoding="utf-8")) if matches else {}
    Path(out_path).write_text(json.dumps(report), encoding="utf-8")
    print(f"[wsl-worker] wrote report ({'found' if matches else 'MISSING'}) -> {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
