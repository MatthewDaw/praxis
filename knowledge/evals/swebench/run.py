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
  ensure a per-instance space and ingest the pre-``base_commit`` window (U3), compute the
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
import hashlib
import json
import queue
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
from typing import Callable

from knowledge.evals.swebench.analyze import aggregate, evaluate_gate, format_report

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "RESULTS.data.json"
DEFAULT_GRADE_CONCURRENCY = 2
DEFAULT_INGEST_WORKERS = 3


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


def _load_instances(n: int, manifest_path: Path | None, *,
                    order: str = "recent", exclude_leaked: bool = True,
                    since: str | None = None):
    """Select+screen ``n`` instances (U1). Re-load a committed manifest when present.

    A ``--manifest`` that exists is read and its rows re-loaded (deterministic rerun);
    otherwise we fetch the SWE-rebench sympy slice and select the top ``n`` by ``order``
    (``recent``/``hard``), dropping verbatim-leaked instances when ``exclude_leaked`` and
    keeping only those created on/after ``since`` when given, and persist the chosen set to
    the manifest. The manifest path only carries the lean selection rows, so re-loading
    fetches the full records again and intersects on ids.
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

    # Load ALL sympy candidates (no limit): SWE-rebench is ordered oldest-first, so a
    # limit would truncate to 2015-era instances whose versions `select` filters out —
    # `select` itself orders and keeps the supported-version top n.
    candidates = load_candidates(fetch_rebench_sympy())
    instances = select(candidates, n, order=order, exclude_leaked=exclude_leaked, since=since)
    if manifest_path is not None:
        write_manifest(instances, manifest_path)
    return instances


def _ensure_sympy_checkout(checkout: Path, *, repo_url: str = "https://github.com/sympy/sympy") -> None:
    """Clone sympy into ``checkout`` if it isn't already a git repo (live-path setup).

    The agent edits a *real* host working tree, so we need sympy checked out there;
    :func:`knowledge.evals.swebench.runner.prepare_checkout` then resets it to the
    instance's ``base_commit`` before each arm. A FULL clone is required (not shallow):
    ``base_commit`` is an arbitrary historical commit a shallow clone wouldn't contain.
    One clone per instance org-dir is simple-but-heavy; a shared clone + ``git worktree``
    is the obvious optimization for a multi-instance run.
    """
    import subprocess

    if (checkout / ".git").is_dir():
        return
    checkout.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", repo_url, str(checkout)], check=True)


def _seed_mcp_cache(space_id: str, *, org: str, base_url: str) -> str:
    """Write a per-instance MCP identity cache (fixed eval org) + space-pinned config.

    Live-path helper (manual). The Praxis MCP server resolves its **org** from the cache
    file at ``PRAXIS_MCP_CACHE`` (the fixed eval org), and its **space** from the
    ``PRAXIS_SPACE`` env override that :func:`build_mcp_config` sets — so each treatment
    agent reads its own instance's private working graph (``X-Praxis-Space``) without
    touching org or login. The cache's own ``space_id`` stays empty; ``PRAXIS_SPACE`` wins.
    Returns the path to a ``--mcp-config`` JSON written alongside.
    """
    from knowledge.evals.swebench.runner import build_mcp_config

    cache_dir = HERE / ".mcp-cache"
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / f"{space_id}.json"
    cache_file.write_text(
        json.dumps(
            {"refresh_token": "", "sub": "dev-user", "email": None,
             "org_id": org, "space_id": "", "api_base": base_url}
        ),
        encoding="utf-8",
    )
    mcp_config = build_mcp_config(space_id, cache_path=str(cache_file))
    config_file = cache_dir / f"{space_id}.mcp.json"
    config_file.write_text(json.dumps(mcp_config), encoding="utf-8")
    return str(config_file)


# --------------------------------------------------------------------------- #
# Parallel-safety: per-arm worktree isolation + a grade-concurrency cap.
# These two make `--workers > 1` safe on a memory-bounded box — without them,
# concurrent arms of the SAME instance would edit one shared checkout (corrupt
# patches) and an unbounded number of heavy WSL Docker grades could OOM.
# --------------------------------------------------------------------------- #
class WorktreePool:
    """A fixed set of git worktrees handed out to concurrent arms, one at a time.

    Each concurrently-running arm needs its own working tree so two agents never edit
    the same files. The pool is sized to the worker count, so disk stays bounded to a
    handful of trees regardless of how many (instance, trial) jobs there are. The
    worktrees all share ONE clone's object store, so adding them is cheap. ``acquire``
    blocks until a tree is free (a plain thread-safe queue); the caller resets the tree
    to its instance's ``base_commit`` (via ``prepare_checkout``) before using it.
    """

    def __init__(self, paths: list[Path]):
        self.paths = list(paths)
        self._free: "queue.Queue[Path]" = queue.Queue()
        for p in self.paths:
            self._free.put(p)

    def acquire(self) -> Path:
        return self._free.get()

    def release(self, path: Path) -> None:
        self._free.put(path)


def _build_worktree_pool(base_clone: Path, root: Path, size: int) -> WorktreePool:
    """Ensure ``size`` git worktrees off ``base_clone`` and return them as a pool.

    Live-path setup (manual). One full sympy clone holds every instance's history, so a
    single worktree can be reset to ANY instance's ``base_commit``; we make ``size`` of
    them (= worker count) as reusable, instance-agnostic working trees. Idempotent: a
    worktree that already exists is reused, and ``git worktree prune`` first clears any
    registrations orphaned by a prior interrupted run.
    """
    _ensure_sympy_checkout(base_clone)  # one shared full clone (all instances' commits)
    subprocess.run(["git", "worktree", "prune"], cwd=str(base_clone), check=False)
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i in range(max(1, size)):
        wt = root / f"wt-{i}"
        if not (wt / ".git").exists():  # worktree .git is a gitdir-pointer FILE
            subprocess.run(["git", "worktree", "add", "--detach", str(wt)],
                           cwd=str(base_clone), check=True)
        paths.append(wt)
    return WorktreePool(paths)


def make_grade_fn(grade_one: Callable[..., object], *, concurrency: int) -> Callable[..., object]:
    """Wrap a ``(instance, patch, *, run_id) -> GradeResult`` grader for safe concurrency.

    Two thread-safe guarantees layered on the raw grader so ``workers > 1`` is sound:

    * **Concurrency cap** — at most ``concurrency`` DISTINCT grades run at once. Each grade
      is a heavy WSL Docker container (builds + runs sympy's tests); on a 16 GB box more
      than ~2 at a time risks OOM, so this semaphore is the throttle independent of the
      agent worker count.
    * **Identical-patch dedup, race-free** — two arms that produced a byte-identical patch
      for the same instance share ONE grade (the original patch-hash ``run_id`` keying).
      A per-``run_id`` lock makes the second caller wait and reuse the cached result rather
      than racing to build the same swebench run directory.
    """
    sem = threading.Semaphore(max(1, concurrency))
    cache: dict[str, object] = {}
    key_locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()

    def grade_fn(instance, patch):
        run_id = f"praxis_{instance.instance_id}_{hashlib.sha1(patch.encode('utf-8')).hexdigest()[:10]}"
        with locks_guard:
            lock = key_locks.setdefault(run_id, threading.Lock())
        with lock:  # serialize identical (instance, patch); distinct run_ids never contend here
            if run_id in cache:
                return cache[run_id]
            with sem:  # cap concurrent DISTINCT Docker grades
                result = grade_one(instance, patch, run_id=run_id)
            cache[run_id] = result
            return result

    return grade_fn


def _prepare_instances(instances: list, *, prepare_one: Callable, ingest_workers: int) -> list:
    """Run ``prepare_one(inst)`` for every instance: warm up serially, then fan out.

    The per-instance pre-arm work (ingest its window, compute R_exist, seed its MCP cache)
    is independent across instances — each owns a private space — so it parallelizes well,
    and it's LLM/IO-bound (the backend distills each PR), so concurrency cuts wall-clock far
    more than CPU count would suggest. The FIRST instance runs alone, because it's the call
    that first-time-creates the shared eval *org*; distinct spaces never race, but a
    concurrent first org-create could. Once it exists, the rest fan out under a bounded pool
    sized to ``ingest_workers`` (kept modest — one local backend + a shared API quota is the
    real ceiling, not cores). The first worker exception propagates so the caller can
    translate a down-backend connection error into the friendly message.
    """
    if not instances:
        return []
    results = [prepare_one(instances[0])]  # warmup: creates the eval org before any concurrency
    rest = instances[1:]
    if rest and ingest_workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=ingest_workers) as pool:
            results.extend(pool.map(prepare_one, rest))
    else:
        results.extend(prepare_one(inst) for inst in rest)
    return results


def run_live(*, n_instances: int, trials: int, k_rework: int, manifest_path: Path | None,
             out_path: Path | None, workers: int = 1,
             order: str = "recent", exclude_leaked: bool = True,
             since: str | None = None,
             grade_concurrency: int = DEFAULT_GRADE_CONCURRENCY,
             ingest_workers: int = DEFAULT_INGEST_WORKERS) -> int:
    """Full pipeline. Raises :class:`BackendUnreachable` (not a traceback) if the backend is down."""
    from knowledge.evals.swebench.experiment import run_experiment
    from knowledge.evals.swebench.grader import grade as grade_instance
    from knowledge.evals.swebench.ingest import (
        EVAL_ORG,
        UrllibClient,
        make_repo_fetcher,
        run_ingest,
        space_id_for,
    )
    from knowledge.evals.swebench.relevance import r_exist
    from knowledge.evals.swebench.runner import prepare_checkout

    client = UrllibClient()
    fetch = make_repo_fetcher(REPO)

    instances = _load_instances(n_instances, manifest_path, order=order,
                                exclude_leaked=exclude_leaked, since=since)
    if not instances:
        print("no instances selected (empty SWE-rebench sympy slice or manifest)", file=sys.stderr)
        return 1

    # Per-instance: ingest the pre-base_commit window and compute the pre-treatment
    # R_exist oracle. The FIRST backend call is where a down backend shows up — wrap it
    # so the user gets the praxis-up hint, not a stack trace.
    rexist_map: dict[str, dict] = {}
    mcp_configs: dict[str, str] = {}
    ingest_results: dict[str, object] = {}

    def prepare_one(inst):
        # All independent pre-arm work for ONE instance (own space): ingest the
        # pre-base_commit window, compute the pre-treatment R_exist oracle, and seed a
        # per-instance MCP cache pinning that instance's SPACE for the treatment arm. The
        # checkout the agent edits is NOT per-instance (shared worktree pool, below); only
        # the space pin is. Returns a tuple assembled into the dicts after the pool joins,
        # so no concurrent dict writes.
        ingest_result = run_ingest(inst, client=client, fetch=fetch)
        rel = r_exist(inst, client)
        space_id = space_id_for(inst)
        mcp = _seed_mcp_cache(space_id, org=EVAL_ORG, base_url=client.base_url)
        return inst.instance_id, ingest_result, {"r_exist": rel.r_exist, "top_score": rel.top_score}, mcp

    # The FIRST backend call is where a down backend shows up — wrap the whole prepare
    # phase so the user gets the praxis-up hint, not a stack trace. Ingestion fans out
    # across instances (LLM/IO-bound, independent spaces); see _prepare_instances.
    try:
        prepared = _prepare_instances(instances, prepare_one=prepare_one,
                                      ingest_workers=ingest_workers)
    except Exception as exc:  # noqa: BLE001 — translate connection failures to a clear message
        if _is_connection_error(exc) or _is_connection_error(exc.__cause__ or exc):
            raise _friendly_backend_error(client.base_url) from exc
        raise

    for iid, ingest_result, rexist_entry, mcp in prepared:
        ingest_results[iid] = ingest_result
        rexist_map[iid] = rexist_entry
        mcp_configs[iid] = mcp

    # One shared sympy clone + a worktree pool sized to the worker count, so concurrent
    # arms each edit an isolated working tree (built AFTER the ingest guard so a down
    # backend still short-circuits before any slow clone). The grade callback is wrapped
    # for a concurrency cap + race-free dedup. Together these make workers>1 safe.
    pool = _build_worktree_pool(
        HERE / ".checkouts" / "_base", HERE / ".checkouts" / "worktrees", workers)
    grade_fn = make_grade_fn(grade_instance, concurrency=grade_concurrency)

    def ingest_fn(inst):  # ingest already ran in the pre-loop; hand U6 the captured
        return ingest_results[inst.instance_id]  # IngestResult so facts_ingested reaches the meta

    # U6 calls run_arm(instance, arm, grade=..., checkout=..., mcp_config_path=..., ...).
    # The treatment arm reads mcp_config_path; control ignores it. Each call borrows an
    # isolated worktree from the pool (blocking until one is free), resets it to the
    # instance's base_commit, runs, and returns the tree — so two concurrent arms, even of
    # the same instance, never share a working tree.
    from knowledge.evals.swebench.runner import run_arm as _run_arm

    def run_arm_wired(instance, arm, **kw):
        worktree = pool.acquire()
        try:
            prepare_checkout(instance, worktree)
            return _run_arm(
                instance, arm,
                checkout=worktree,
                mcp_config_path=mcp_configs[instance.instance_id],
                **kw,
            )
        finally:
            pool.release(worktree)

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
                        help="concurrent (instance, trial) jobs on the live path (default 1); "
                             "each arm gets an isolated git worktree so >1 is safe")
    parser.add_argument("--grade-concurrency", type=int, default=DEFAULT_GRADE_CONCURRENCY,
                        help=f"max concurrent WSL Docker grades regardless of --workers "
                             f"(default {DEFAULT_GRADE_CONCURRENCY}; drop to 1 if WSL OOMs on 16 GB)")
    parser.add_argument("--ingest-workers", type=int, default=DEFAULT_INGEST_WORKERS,
                        help=f"concurrent per-instance ingests in the (LLM/IO-bound) pre-loop "
                             f"(default {DEFAULT_INGEST_WORKERS}; first instance warms up serially)")
    parser.add_argument("--from-records", type=Path, default=None,
                        help="OFFLINE: aggregate a committed records JSON instead of a live run")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="live: read/write the chosen-instance manifest for deterministic reruns")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="where to write records + report + gate (default RESULTS.data.json)")
    parser.add_argument("--order", choices=("recent", "hard"), default="recent",
                        help="instance selection order: 'recent' (newest) or 'hard' "
                             "(biggest gold patch — biases toward bugs control may fail; default recent)")
    parser.add_argument("--include-leaked", action="store_true",
                        help="keep verbatim-leaked instances (issue pastes a fix line); "
                             "excluded by default")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="keep only instances created on/after this date — the "
                             "least-contaminated slice; composes with --order hard")
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
            order=args.order,
            exclude_leaked=not args.include_leaked,
            since=args.since,
            grade_concurrency=args.grade_concurrency,
            ingest_workers=args.ingest_workers,
        )
    except BackendUnreachable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
