"""U8: end-to-end CLI for the SWE-rebench PR-knowledge pilot.

Wires U1–U7 into one entry point with two paths, mirroring
``cases/dom/pr_knowledge_dogfood/analyze.py``'s argparse ``main`` + ``--from-records``:

* ``--from-records FILE`` — OFFLINE, analysis-only. Loads a committed records dict
  (the ``tests/fixtures/records.sample.json`` shape: ``{"records", "rexist_map",
  "instances"}``), re-aggregates it through U7 (:func:`aggregate` →
  :func:`evaluate_gate` → :func:`format_report`), prints the report, and writes
  ``{"records", "report", "gate", "rexist_map"}`` to ``--out``. No backend, no
  Docker, no ``claude`` — this is the path the offline tests exercise.

* live run (``--instances N --trials K``) — orchestrates the full pipeline per the
  plan's High-Level Technical Design: select+screen instances (U1), per instance
  build a fresh org and ingest the pre-``base_commit`` window (U3), compute the
  pre-treatment ``R_exist`` oracle (U4), then run ``trials`` × (treatment + control)
  arms (U5) each graded by the arm64 swebench harness (U2) through U6's
  orchestration, and finally aggregate (U7). This path needs the Praxis backend up
  (dev tenant) and WSL2 Docker; see README.md. It is the manual/outside-CI path and
  is intentionally not unit-tested.

A missing backend (UrllibClient connection refused on the first call) surfaces a
clear, actionable message — never a raw traceback.

    uv run python -m knowledge.evals.swebench.run --instances 10 --trials 3
    uv run python -m knowledge.evals.swebench.run --from-records tests/fixtures/records.sample.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path

from knowledge.evals.swebench.analyze import aggregate, evaluate_gate, format_report

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "RESULTS.data.json"


class BackendUnreachable(RuntimeError):
    """The Praxis backend did not answer — raised with a friendly, actionable message."""


def _friendly_backend_error(base_url: str) -> BackendUnreachable:
    return BackendUnreachable(
        f"Praxis backend not reachable at {base_url}; "
        "run the praxis-up skill to bring up the local stack (Postgres + FastAPI on :8000), "
        "then retry."
    )


def _is_connection_error(exc: BaseException) -> bool:
    """True for the family of "backend isn't listening" errors across urllib + sockets."""
    if isinstance(exc, (ConnectionError, urllib.error.URLError)):
        # urllib.error.URLError wraps the underlying socket error in ``.reason``.
        reason = getattr(exc, "reason", None)
        return reason is None or isinstance(reason, (ConnectionError, OSError))
    return isinstance(exc, OSError)


# --------------------------------------------------------------------------- #
# Offline path: re-aggregate a committed records dict (no backend / Docker).
# --------------------------------------------------------------------------- #
def run_from_records(records_path: Path, out_path: Path | None) -> int:
    """Analysis-only path: load the dict, aggregate → gate → report, print + write."""
    data = json.loads(records_path.read_text(encoding="utf-8"))
    records = data["records"]
    rexist_map = data.get("rexist_map", {})
    instances = data.get("instances")

    report = aggregate(records, rexist_map, instances=instances)
    gate = evaluate_gate(report)
    print(format_report(report, gate))

    if out_path is not None:
        _write_results(out_path, records, report, gate, rexist_map)
    return 0


def _write_results(out_path: Path, records, report, gate, rexist_map) -> None:
    out_path.write_text(
        json.dumps(
            {"records": records, "report": report, "gate": gate, "rexist_map": rexist_map},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote results -> {out_path}")


# --------------------------------------------------------------------------- #
# Live path: select -> ingest -> R_exist -> arms -> grade -> analyze.
# This is the manual/outside-CI orchestrator (needs backend + WSL2 Docker).
# --------------------------------------------------------------------------- #
REPO = "sympy/sympy"


def _load_instances(n: int, manifest_path: Path | None):
    """Select+screen ``n`` instances (U1). Re-load a committed manifest when present.

    A ``--manifest`` that exists is read and its rows re-loaded (deterministic rerun);
    otherwise we fetch the SWE-rebench sympy slice and select+screen the top ``n``,
    persisting the chosen set to the manifest for reproducibility. Kept deliberately
    simple — the manifest path only carries the lean selection rows, so re-loading
    fetches the full records again and intersects on the chosen ids.
    """
    from knowledge.evals.swebench.instances import (
        fetch_rebench_sympy,
        load_candidates,
        read_manifest,
        select,
        write_manifest,
    )

    if manifest_path is not None and manifest_path.exists():
        chosen_ids = {row["instance_id"] for row in read_manifest(manifest_path)}
        candidates = load_candidates(fetch_rebench_sympy())
        return [c for c in candidates if c.instance_id in chosen_ids]

    # Fetch a generous superset (versions get filtered in select) and pick the top n.
    candidates = load_candidates(fetch_rebench_sympy(limit=max(n * 5, 50)))
    instances = select(candidates, n)
    if manifest_path is not None:
        write_manifest(instances, manifest_path)
    return instances


def _seed_mcp_cache(org_id: str, *, base_url: str) -> str:
    """Write a per-instance MCP identity cache pinning ``org_id``; return its path.

    Live-path helper (manual). The Praxis MCP server resolves its tenant from the
    cache file at ``PRAXIS_MCP_CACHE`` (see :func:`knowledge.mcp.identity.cache_path`);
    by writing one cache file per instance whose ``org_id`` is the instance's org, each
    treatment agent sends its own ``X-Praxis-Org`` without clobbering another agent. In
    the dev tenant (auth disabled) the login fields are placeholders — the ``org_id`` is
    the load-bearing pin. Returns the path to a ``--mcp-config`` JSON written alongside.
    """
    from knowledge.evals.swebench.runner import build_mcp_config

    cache_dir = HERE / ".mcp-cache"
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / f"{org_id}.json"
    cache_file.write_text(
        json.dumps(
            {"refresh_token": "", "sub": "dev-user", "email": None,
             "org_id": org_id, "api_base": base_url}
        ),
        encoding="utf-8",
    )
    mcp_config = build_mcp_config(org_id, cache_path=str(cache_file))
    config_file = cache_dir / f"{org_id}.mcp.json"
    config_file.write_text(json.dumps(mcp_config), encoding="utf-8")
    return str(config_file)


def run_live(*, n_instances: int, trials: int, k_rework: int, manifest_path: Path | None,
             out_path: Path | None, workers: int = 1) -> int:
    """Full pipeline. Raises :class:`BackendUnreachable` (not a traceback) if the backend is down."""
    from knowledge.evals.swebench.experiment import run_experiment
    from knowledge.evals.swebench.grader import grade as grade_instance
    from knowledge.evals.swebench.ingest import (
        UrllibClient,
        make_repo_fetcher,
        org_id_for,
        run_ingest,
    )
    from knowledge.evals.swebench.relevance import r_exist
    from knowledge.evals.swebench.runner import prepare_checkout

    client = UrllibClient()
    fetch = make_repo_fetcher(REPO)

    instances = _load_instances(n_instances, manifest_path)
    if not instances:
        print("no instances selected (empty SWE-rebench sympy slice or manifest)", file=sys.stderr)
        return 1

    # Per-instance: ingest the pre-base_commit window and compute the pre-treatment
    # R_exist oracle. The FIRST backend call is where a down backend shows up — wrap it
    # so the user gets the praxis-up hint, not a stack trace.
    rexist_map: dict[str, dict] = {}
    checkouts: dict[str, Path] = {}
    mcp_configs: dict[str, str] = {}
    ingest_results: dict[str, object] = {}
    try:
        for inst in instances:
            ingest_results[inst.instance_id] = run_ingest(inst, client=client, fetch=fetch)
            rel = r_exist(inst, client)
            rexist_map[inst.instance_id] = {"r_exist": rel.r_exist, "top_score": rel.top_score}
            # Live arm wiring (manual path): a per-instance checkout reset to base_commit
            # (+ install_config venv, built by the orchestrator before the first arm) and a
            # per-instance MCP cache pinning the org for the treatment arm.
            checkout = (HERE / ".checkouts" / org_id_for(inst))
            checkouts[inst.instance_id] = checkout
            mcp_configs[inst.instance_id] = _seed_mcp_cache(org_id_for(inst), base_url=client.base_url)
    except Exception as exc:  # noqa: BLE001 — translate connection failures to a clear message
        if _is_connection_error(exc) or _is_connection_error(exc.__cause__ or exc):
            raise _friendly_backend_error(client.base_url) from exc
        raise

    def ingest_fn(inst):  # ingest already ran in the pre-loop; hand U6 the captured
        return ingest_results[inst.instance_id]  # IngestResult so facts_ingested reaches the meta

    def grade_fn(inst, patch):
        return grade_instance(inst, patch)

    # U6 calls run_arm(instance, arm, grade=..., checkout=..., mcp_config_path=..., ...).
    # The treatment arm reads mcp_config_path; the control arm ignores it (no Praxis MCP).
    # checkout + mcp_config_path are per-instance, so we wrap run_arm to look them up.
    from knowledge.evals.swebench.runner import run_arm as _run_arm

    def run_arm_wired(instance, arm, **kw):
        prepare_checkout(instance, checkouts[instance.instance_id])
        return _run_arm(
            instance, arm,
            checkout=checkouts[instance.instance_id],
            mcp_config_path=mcp_configs[instance.instance_id],
            **kw,
        )

    exp = run_experiment(
        instances,
        trials=trials,
        grade=grade_fn,
        ingest=ingest_fn,
        run_arm=run_arm_wired,
        k_rework=k_rework,
        workers=workers,
    )

    report = aggregate(exp["records"], rexist_map, instances=exp["instances"])
    gate = evaluate_gate(report)
    print("\n" + format_report(report, gate))
    if out_path is not None:
        _write_results(out_path, exp["records"], report, gate, rexist_map)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="swebench-pilot",
        description="SWE-rebench PR-knowledge pilot: select -> ingest -> arms -> grade -> analyze",
    )
    parser.add_argument("--instances", type=int, default=10,
                        help="number of SWE-rebench sympy instances to run (default 10)")
    parser.add_argument("--trials", type=int, default=3,
                        help="trials per instance per arm (default 3)")
    parser.add_argument("--k-rework", type=int, default=1,
                        help="max repro-test rework rounds per arm (default 1)")
    parser.add_argument("--workers", type=int, default=1,
                        help="concurrent (instance, trial) jobs on the live path (default 1)")
    parser.add_argument("--from-records", type=Path, default=None,
                        help="OFFLINE: aggregate a committed records JSON instead of a live run")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="live: read/write the chosen-instance manifest for deterministic reruns")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="where to write records + report + gate (default RESULTS.data.json)")
    args = parser.parse_args(argv)

    if args.from_records is not None:
        return run_from_records(args.from_records, args.out)

    try:
        return run_live(
            n_instances=args.instances,
            trials=args.trials,
            k_rework=args.k_rework,
            manifest_path=args.manifest,
            out_path=args.out,
            workers=args.workers,
        )
    except BackendUnreachable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
