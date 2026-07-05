"""Build-reproduction eval harness: START commit + ticket-in-Praxis -> run the factory build loop on
the ticket -> score how close the result lands to the GOLDEN completion commit.

    python -m evals.build_repro.run_eval --start-commit <SHA> --golden-commit <SHA> [--run-tests]

Isolated: builds in a temp `git worktree` off START, never touching your team-app working tree. Drives
the headless `claude` CLI on the subscription (no API key) for BOTH the build agent and the judge.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

R73 = "e9db7e1dada846ce843474b7785df515"
_REPO_ROOT = Path(__file__).resolve().parents[2]            # agent_factory/
_DEFAULT_TEAM_APP = _REPO_ROOT.parent / "team-app"


def _load_dotenv(root: Path) -> None:
    env = root / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def _run_build_agent(worktree: str, ticket_cid: str, timeout: int) -> None:
    """Drive the real factory loop headlessly in the worktree (tools enabled, subscription billing)."""
    from evals.plan_repro.claude_cli import _claude_path  # reuse the binary resolver
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)                      # bill the subscription, not an API key
    prompt = f"/agent-factory:af-build please implement R73 {ticket_cid}"
    print(f"  driving build agent in {worktree} ...", flush=True)
    proc = subprocess.run(
        [_claude_path(), "-p", prompt, "--permission-mode", "bypassPermissions"],
        cwd=worktree, env=env, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
    )
    print(f"  build agent exited {proc.returncode}", flush=True)
    if proc.returncode != 0:
        print("  (build agent stderr tail)\n" + (proc.stderr or "")[-800:], flush=True)


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    _load_dotenv(_REPO_ROOT)
    praxis_repo = _REPO_ROOT.parent / "praxis"
    if praxis_repo.is_dir():
        sys.path.insert(0, str(praxis_repo))
    sys.path.insert(0, str(_REPO_ROOT / "hooks"))
    sys.path.insert(0, str(_REPO_ROOT))

    p = argparse.ArgumentParser(description="Build-reproduction eval for a factory ticket.")
    p.add_argument("--repo", default=str(_DEFAULT_TEAM_APP), help="team-app checkout")
    p.add_argument("--start-commit", required=True, help="checkpoint to build FROM (pre-feature)")
    p.add_argument("--golden-commit", required=True, help="known-good completion commit (reference)")
    p.add_argument("--ticket", default=R73, help="requirement fact id (default R73)")
    p.add_argument("--run-tests", action="store_true", help="run the golden's e2e test (needs the stack)")
    p.add_argument("--threshold", type=float, default=0.8)
    p.add_argument("--agent-timeout", type=int, default=3600)
    args = p.parse_args(argv)

    import _praxis as px
    import _ticket_state as ts
    from evals.build_repro import score as sc
    from evals.plan_repro.claude_cli import make_claude_cli_complete

    repo = str(Path(args.repo).resolve())
    wt = str(_REPO_ROOT / ".build-repro-worktree")

    # 1) ISOLATED checkout of START.
    _git(repo, "worktree", "prune")
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt],
                   capture_output=True, text=True)
    print(f"setup: worktree {wt} @ {args.start_commit[:10]}", flush=True)
    add = subprocess.run(["git", "-C", repo, "worktree", "add", "--force", "--detach", wt,
                          args.start_commit], capture_output=True, text=True, encoding="utf-8")
    if add.returncode != 0:
        print("FAILED to create worktree:\n" + add.stderr); return 2

    try:
        # 2) RESET the ticket to pristine incomplete in Praxis (hard enum + clear lease/pins/
        #    coverage-contract/run-marker so a re-run starts from a clean slate).
        px.patch_meta(args.ticket, {ts.M_BUILD_STATE: "incomplete", ts.M_BLOCK_REASON: None,
                                    ts.M_CLAIM_OWNER: None, ts.M_CLAIM_AT: None,
                                    ts.M_CLAIM_HEARTBEAT_AT: None, ts.M_CLAIM_LEASE_TTL: None,
                                    ts.M_REQUIRED_VALIDATIONS: [], ts.M_PINNED_CHECKS: [],
                                    ts.M_RUN_OWNER: None, ts.M_RUN_AT: None, ts.M_RUN_SCOPE: None})
        print("reset: ticket -> build_state=incomplete, no lease, no pins, no run marker", flush=True)

        # 3) RUN the loop (the autonomous build).
        _run_build_agent(wt, args.ticket, args.agent_timeout)

        # 4) DIFFS — golden (reference) and agent (produced), restricted to golden's touched paths.
        golden_files = sc.touched_files(repo, args.start_commit, args.golden_commit)
        subprocess.run(["git", "-C", wt, "add", "-A"], capture_output=True, text=True)
        agent_files = sc.touched_files(wt, args.start_commit)
        paths = sorted(golden_files)
        golden_diff = sc.git_diff(repo, args.start_commit, args.golden_commit, paths=paths)
        agent_diff = sc.git_diff(wt, args.start_commit, paths=paths) or sc.git_diff(wt, args.start_commit)

        # 5) PRAXIS state after the loop.
        meta = (px.get_fact(args.ticket).get("meta") or {})
        pins = meta.get(ts.M_PINNED_CHECKS) or []
        praxis_state = {"build_state": meta.get(ts.M_BUILD_STATE),
                        "check_passed": bool(pins) and all(c.get("passed") for c in pins)}

        # 6) SCORE.
        judge = make_claude_cli_complete(timeout=150)
        result = sc.score_build(judge, golden_diff, agent_diff, agent_files, golden_files,
                                praxis_state, threshold=args.threshold)

        # 7) BEHAVIORAL (optional, authoritative when green).
        if args.run_tests:
            t = subprocess.run(["npx", "playwright", "test", "e2e/auth/password-reset"],
                               cwd=wt, capture_output=True, text=True, encoding="utf-8")
            result.behavioral = {"passed": t.returncode == 0, "detail": (t.stdout or t.stderr)[-300:]}
            if result.behavioral["passed"]:
                result.passed = True

        print("\n" + result.format(), flush=True)
        return 0 if result.passed else 1
    finally:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt],
                       capture_output=True, text=True)
        print("teardown: worktree removed", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
