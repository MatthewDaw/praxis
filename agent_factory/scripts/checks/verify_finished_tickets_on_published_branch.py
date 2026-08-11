#!/usr/bin/env python3
"""OPS-12 (canonical factory driver): a ticket cannot be finished while its code exists only in a
throwaway worktree.

A ticket may only reach ``build_state: finished`` once the code satisfying it is present on the
published integration branch -- a worker that builds and verifies inside a throwaway worktree must
not be able to record success while its commits exist nowhere but that worktree. For every
requirement whose ``build_state`` is ``finished`` this audit resolves the file paths each pinned
check's ``run`` command reads or executes against, and confirms those paths are reachable from the
published branch (default ``origin/af-build/<project>``). It exits non-zero, naming the offending
ticket(s)/path(s), the moment a finished ticket's checked files are genuinely never published.

Why this is the CANONICAL driver (and not a per-project copy): the earlier project-local copy
declared a path "absent" whenever :func:`extract_target_paths` returned a token the audit could not
resolve, which drowned the handful of genuine never-published pins under two large classes of
NOISE, both artifacts of HOW the ``run`` command was recorded rather than of a real gap:

  1. Unexpanded ``$VAR`` placeholders -- a recorded ``run`` body like ``pytest $TESTDIR/foo.py``
     stored the literal ``$TESTDIR`` (never expanded at record time). ``$TESTDIR/foo.py`` is not a
     path that could ever exist on any branch, so it was reported as "absent" every time. These are
     now expanded from the environment when possible, and otherwise LABELLED as unresolved
     placeholders -- never counted as never-published.
  2. Absolute paths inside since-deleted worktrees -- e.g.
     ``/workspace/proj/.claude/worktrees/agent-xyz/tests/test_foo.py``. That absolute path never
     exists on the branch (the branch stores repo-relative paths), but the FILE it names very often
     does. These are now resolved against the merged tree by repo-relative suffix (the part after
     the ``worktrees/<id>/`` segment, or relative to the repo root) and then by basename, before any
     "absent" verdict.

Only a path that resolves to a real repo-relative candidate AND is still not reachable from the
branch is reported as ``never-published`` -- the high-signal category the gate fails closed on.

Usage
-----
    python -m ... verify_finished_tickets_on_published_branch.py --project <name>
        [--branch origin/af-build/<project>] [--repo-root PATH] [--tickets-json PATH | -]

``--tickets-json`` points at a JSON array of ticket facts (each with ``build_state`` and
``pinned_checks``) -- for tests and offline runs, bypassing the live Praxis query. Pass ``-`` to
read the array from stdin.

Live mode (no ``--tickets-json``) requires ``PRAXIS_API_KEY`` and ``PRAXIS_API_BASE_URL`` in the
environment (``PRAXIS_ORG`` optional, defaults to ``agent-factory``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_KNOWN_EXTS = {
    "py", "ts", "tsx", "js", "jsx", "sh", "yml", "yaml", "json", "txt", "md", "cfg", "ini", "toml",
}
# A real repo-relative file path OR an absolute/placeholder one: word chars / dots / slashes /
# dashes / ``$`` (for ``$VAR`` placeholders) / a leading slash (absolute worktree paths), ending in
# a known extension. This shape excludes shell flags ("-rnB4") and quoted grep/regex patterns
# ("@router\.(post|put)") -- neither matches.
_PATH_RE = re.compile(r"^[\w$/][\w./$-]*\.(?:%s)$" % "|".join(sorted(_KNOWN_EXTS)))

# The throwaway-worktree segment every af-build worker roots its checkout under. The repo-relative
# part of an absolute worktree path is everything AFTER the ``worktrees/<id>/`` prefix.
_WORKTREE_SEGMENT = "worktrees"


class PraxisAuditError(RuntimeError):
    """Raised when the live ticket set cannot be fetched or a branch ref cannot be resolved."""


_OPERATORS = {"&&", "||", ";", "|"}


def extract_target_paths(run_cmd: str, base_dir: Path = Path(".")) -> list[str]:
    """Best-effort extraction of the paths a pinned check's ``run`` shell command reads or executes
    against. Returns tokens verbatim (still possibly ``$VAR``-prefixed or absolute) -- classification
    and resolution happen in :func:`classify_path`, so the raw evidence is preserved for labelling.

    Not a shell parser: tokenizes with quoting honored, splits on unquoted ``&&``/``||``/``;``/``|``,
    tracks a leading ``cd DIR`` to rebase subsequent relative paths, and keeps only tokens shaped
    like a real file (a dotted, known-extension path) -- excluding flags and quoted patterns.
    """
    lexer = shlex.shlex(run_cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        all_tokens = list(lexer)
    except ValueError:
        return []

    cwd = base_dir
    paths: list[str] = []
    sub_command: list[str] = []

    def _flush(tokens: list[str]) -> None:
        nonlocal cwd
        if not tokens:
            return
        if tokens[0] == "cd" and len(tokens) > 1:
            cwd = cwd / tokens[1]
            return
        for tok in tokens:
            if tok.startswith("-") or "." not in tok or not _PATH_RE.match(tok):
                continue
            # A ``$VAR``- or ``/``-anchored token is NOT rebased onto ``cwd`` (it is already
            # absolute or a placeholder); a plain relative token is.
            if tok.startswith("$") or tok.startswith("/"):
                paths.append(tok)
            else:
                paths.append(os.path.normpath(str(cwd / tok)))
    for tok in all_tokens:
        if tok in _OPERATORS:
            _flush(sub_command)
            sub_command = []
        else:
            sub_command.append(tok)
    _flush(sub_command)
    return paths


def _repo_relative_candidates(path: str, repo_root: Path) -> list[str]:
    """The repo-relative path candidate(s) an extracted token might name on the branch, most
    specific first. Resolves the two NOISE classes so the gate never mislabels them absent:

      * an absolute path passing through ``.claude/worktrees/<id>/`` -> the suffix after that
        segment (the real repo-relative path the throwaway worktree mirrored);
      * any other absolute path under ``repo_root`` -> its path relative to the repo root.

    A plain relative token is already a candidate as-is.
    """
    p = PurePosixPath(path)
    parts = p.parts
    if _WORKTREE_SEGMENT in parts:
        i = parts.index(_WORKTREE_SEGMENT)
        # parts[i+1] is the worktree id; the repo-relative path is everything after it.
        suffix = parts[i + 2:]
        if suffix:
            return [str(PurePosixPath(*suffix))]
        return []
    if path.startswith("/"):
        try:
            return [str(Path(path).resolve().relative_to(repo_root.resolve()))]
        except ValueError:
            # Absolute, but not under this repo root and not a worktree path -- fall back to the
            # basename lane only (handled by the caller); no repo-relative suffix to offer.
            return []
    return [os.path.normpath(path)]


def _ref_exists(repo_root: Path, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _blob_exists(repo_root: Path, ref: str, path: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{ref}:{path}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def _branch_basenames(repo_root: Path, ref: str) -> set[str]:
    """Every basename present anywhere in the branch's merged tree -- the last-resort resolution
    lane for a worktree path whose repo-relative suffix has moved but whose file is still published
    (a rename/relocation, which is NOT a never-published gap)."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", ref],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if proc.returncode != 0:
        return set()
    return {PurePosixPath(line).name for line in proc.stdout.splitlines() if line.strip()}


# --------------------------------------------------------------------------- classification

# A token is an unresolved placeholder if, AFTER a best-effort env expansion, it still carries a
# ``$``: the recorded ``run`` body stored a variable that was never expanded, so it can never name
# a real branch path -- label it, never call it absent.
def classify_path(
    token: str, repo_root: Path, branch: str, branch_basenames: set[str],
) -> tuple[str, str]:
    """Classify ONE extracted token into ``(category, detail)`` where category is one of
    ``"present"`` (reachable from the branch, no problem), ``"placeholder"`` (unexpanded ``$VAR`` --
    labelled, non-fatal), or ``"never_published"`` (resolved to a real candidate that is genuinely
    not on the branch -- the fatal, high-signal category)."""
    expanded = os.path.expandvars(token)
    if "$" in expanded:
        return "placeholder", token

    candidates = _repo_relative_candidates(expanded, repo_root)
    for cand in candidates:
        if _blob_exists(repo_root, branch, cand):
            return "present", cand
    # Repo-relative suffix missed (or there was none for a foreign-absolute path): try the basename
    # lane before declaring a never-published gap -- a relocated-but-published file is not a gap.
    basename = PurePosixPath(expanded).name
    if basename in branch_basenames:
        return "present", f"{token} (resolved by basename {basename!r})"
    # Genuinely never published: prefer the resolved repo-relative candidate in the message so the
    # human sees the real path to look for, not the throwaway-worktree absolute one.
    detail = candidates[0] if candidates else token
    return "never_published", detail


# --------------------------------------------------------------------------- live fetch / load

def _fetch_finished_tickets_live(project: str) -> list[dict]:
    """Query Praxis directly (stdlib only) for this project's ``prd-<project>`` requirement facts
    with ``build_state: finished``, using the ``x-praxis-key`` auth path."""
    api_base = os.environ.get("PRAXIS_API_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("PRAXIS_API_KEY", "").strip()
    if not api_base or not api_key:
        raise PraxisAuditError(
            "live mode needs PRAXIS_API_BASE_URL and PRAXIS_API_KEY in the environment "
            "(or pass --tickets-json for an offline run)"
        )
    org = os.environ.get("PRAXIS_ORG", "agent-factory").strip()
    params = {
        "state": "any",
        "category": "requirement",
        "meta": json.dumps({"build_state": "finished"}),
    }
    url = f"{api_base}/facts/by?" + urllib.parse.urlencode(params)
    headers = {
        "x-praxis-key": api_key,
        "x-praxis-org": org,
        "x-praxis-space": project,
        "x-praxis-snapshot": f"prd-{project}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001 - fail closed with a clear message
        raise PraxisAuditError(f"Praxis GET /facts/by failed: {exc}") from exc
    facts = body.get("facts") or []
    return [f.get("meta", f) for f in facts]


def _load_tickets(tickets_json: str | None, project: str) -> list[dict]:
    if tickets_json is None:
        return _fetch_finished_tickets_live(project)
    text = sys.stdin.read() if tickets_json == "-" else Path(tickets_json).read_text()
    data = json.loads(text)
    if not isinstance(data, list):
        raise PraxisAuditError("--tickets-json must contain a JSON array of ticket facts")
    return data


# --------------------------------------------------------------------------- audit

def audit(tickets: list[dict], repo_root: Path, branch: str) -> dict[str, list[str]]:
    """Return the audit result partitioned by signal:

      * ``never_published`` -- finished tickets whose pinned check file is genuinely absent from the
        branch (the gate fails closed on a non-empty list here);
      * ``placeholder`` -- pins whose recorded ``run`` carried an unexpanded ``$VAR`` (labelled,
        non-fatal noise);
      * ``resolved`` -- pins that only looked absent because they named a since-deleted worktree
        path, but whose file IS reachable from the branch (labelled, non-fatal).
    """
    branch_basenames = _branch_basenames(repo_root, branch)
    result: dict[str, list[str]] = {"never_published": [], "placeholder": [], "resolved": []}
    for ticket in tickets:
        if ticket.get("build_state") != "finished":
            continue
        req_id = ticket.get("requirement_id") or ticket.get("id") or "<unknown-ticket>"
        for check in ticket.get("pinned_checks") or []:
            meta = check.get("meta") or {}
            run_cmd = meta.get("run") or check.get("run")
            if not run_cmd:
                continue
            check_id = meta.get("check_id") or check.get("validation_id") or "<unknown-check>"
            for token in extract_target_paths(run_cmd):
                category, detail = classify_path(token, repo_root, branch, branch_basenames)
                if category == "present":
                    continue
                line = f"{req_id} ({check_id}): {detail}"
                if category == "never_published":
                    result["never_published"].append(f"{line} -- NOT on {branch}")
                elif category == "placeholder":
                    result["placeholder"].append(
                        f"{line} -- unexpanded $VAR in recorded run command (not resolvable)")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--branch", default=None,
                        help="Published integration branch (default origin/af-build/<project>).")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--tickets-json", default=None,
        help="Path to a JSON array of ticket facts, or '-' for stdin; bypasses the live Praxis "
             "query (for tests / offline runs).",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    branch = args.branch or f"origin/af-build/{args.project}"

    if not _ref_exists(repo_root, branch):
        sys.stderr.write(f"cannot resolve branch ref {branch!r} in {repo_root} -- fetch it first\n")
        return 2

    try:
        tickets = _load_tickets(args.tickets_json, args.project)
    except PraxisAuditError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    result = audit(tickets, repo_root, branch)
    never = result["never_published"]
    placeholders = result["placeholder"]

    # Low-signal categories print to stdout as labelled notes -- never to the failing channel.
    if placeholders:
        sys.stdout.write(
            f"note: {len(placeholders)} pinned check(s) carry an unexpanded $VAR path in their "
            f"recorded run command -- skipped (not treated as absent):\n")
        for line in placeholders:
            sys.stdout.write(f"  ~ {line}\n")

    if never:
        sys.stderr.write(
            f"FINISHED-TICKET BRANCH AUDIT: BLOCKED -- {len(never)} finished ticket check file(s) "
            f"never published to {branch}:\n")
        for line in never:
            sys.stderr.write(f"  - {line}\n")
        return 1

    sys.stdout.write(
        f"FINISHED-TICKET BRANCH AUDIT: CLEAR "
        f"({len(tickets)} finished ticket(s) checked against {branch}; "
        f"{len(placeholders)} placeholder pin(s) skipped)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
