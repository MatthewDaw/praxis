"""U3: per-instance org ingest — a point-in-time Praxis snapshot for one instance.

WHY a *fresh org per instance*: the org becomes a point-in-time snapshot holding
only PRs merged before the instance's ``base_commit``. Ingesting **oldest-first**
means any in-window contradictions resolve to the latest-as-of-``base_commit``
value (the stock ingest path's ``auto_resolve`` keeps the newest writer). The
instance's fix-PR — and any PR whose diff merely restates the gold diff — is
**excluded**, and a leakage guard fails loudly if any ingested fact restates the
gold diff, so the snapshot can never leak the answer into the treatment arm.
Per-instance orgs also isolate instances from each other (R5): each ingest posts
only its own PRs under its own ``X-Praxis-Org``.

Two seams keep the whole pipeline offline-testable, mirroring ``pr_source.py``'s
injected ``Fetcher``:

* :data:`Fetcher` — the argv→stdout ``gh``/``git`` callable. We wrap
  :func:`knowledge.injestion.pr_source.default_fetcher` with
  :func:`make_repo_fetcher` so PRs are fetched from ``sympy/sympy`` (``gh -R``),
  not the cwd. Tests inject a fake that switches on argv and returns fixture JSON.
* :class:`HttpClient` — a tiny injectable client for ``POST /orgs``,
  ``POST /ingest`` and ``GET /context``. The default :class:`UrllibClient` uses
  ``urllib`` (no extra dependency, mirrors the proven smoke driver); tests inject
  a fake that records calls and returns canned responses. **No real HTTP in tests.**

The cutoff date is :attr:`Instance.created_at` (the instance's issue/commit date) —
the pragmatic point-in-time bound available offline; see :func:`select_window`.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from knowledge.injestion.pr_source import (
    Fetcher,
    build_pr_document,
    default_fetcher,
)
from knowledge.evals.swebench.instances import Instance

# Default dev backend (PRAXIS_AUTH_DISABLED=1 → no login needed); the org owner is
# the dev tenant `dev-user`, who is the auto-member that `active_org` authorizes.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# Dev-tenant org password (auth is disabled in dev; the value is required but unused
# for membership since the creator is the auto-member).
_ORG_PASSWORD = "swebench-eval"

# How many merged PRs to pull from `gh pr list` before date-filtering to the window.
# A generous cap so the pre-base_commit window isn't truncated by a too-small limit.
DEFAULT_LIST_LIMIT = 200


# ---------------------------------------------------------------------------
# Seam 1: the gh/git fetcher, wrapped to target a fixed repo.
# ---------------------------------------------------------------------------
def make_repo_fetcher(repo: str, base: Fetcher = default_fetcher) -> Fetcher:
    """Wrap ``base`` so every ``gh`` argv targets ``repo`` (``gh -R <repo> ...``).

    ``pr_source`` issues cwd-relative ``gh`` calls; this injects ``-R sympy/sympy``
    so the PRs come from the sympy repo, not the eval's checkout. Mirrors the
    proven smoke-#2 ``fetch_R`` wrapper. ``git`` argv pass through untouched.
    """

    def fetch(argv: list[str]) -> str:
        if argv and argv[0] == "gh":
            argv = argv + ["-R", repo]
        return base(argv)

    return fetch


# ---------------------------------------------------------------------------
# Seam 2: the HTTP client for the Praxis backend.
# ---------------------------------------------------------------------------
class HttpClient(Protocol):
    """The three backend calls U3 needs; a fake implements the same shape offline."""

    def post_orgs(self, body: dict) -> dict: ...

    def post_ingest(self, org_id: str, body: dict) -> dict: ...

    def get_context(self, org_id: str, query: str, top_k: int) -> dict: ...


class OrgConflict(Exception):
    """``POST /orgs`` returned 409 — the org already exists (idempotent create)."""


@dataclass
class UrllibClient:
    """Default :class:`HttpClient` over ``urllib`` (mirrors the smoke driver)."""

    base_url: str = DEFAULT_BASE_URL

    def _call(self, method: str, path: str, *, org_id: str | None = None,
              body: dict | None = None, params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if org_id is not None:
            headers["X-Praxis-Org"] = org_id
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)

    def post_orgs(self, body: dict) -> dict:
        import urllib.error

        try:
            return self._call("POST", "/orgs", body=body)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise OrgConflict(body.get("orgId", "")) from exc
            raise

    def post_ingest(self, org_id: str, body: dict) -> dict:
        return self._call("POST", "/ingest", org_id=org_id, body=body)

    def get_context(self, org_id: str, query: str, top_k: int) -> dict:
        return self._call("GET", "/context", org_id=org_id,
                          params={"query": query, "top_k": str(top_k)})


# ---------------------------------------------------------------------------
# Result record.
# ---------------------------------------------------------------------------
@dataclass
class IngestResult:
    """What one instance's ingest produced — the per-instance ingest record.

    ``ingestion_cost`` is ``None``: ``POST /ingest`` surfaces fact/merge/conflict
    *counts*, not a token or USD cost, so there is no real number to record here.
    The amortized ingestion-cost line is a separate metric (a distillation-cost
    probe in a later unit); fabricating a number here would corrupt it, so the
    field is a present-but-``None`` placeholder (the test asserts presence, not a
    value). ``facts_ingested`` is the real signal /ingest does surface.
    """

    org_id: str
    pr_numbers: list[int]
    ingested: int  # documents POSTed (one per selected PR)
    ingestion_cost: float | None = None
    facts_ingested: int = 0
    actions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline functions.
# ---------------------------------------------------------------------------
_SANITIZE = re.compile(r"[^A-Za-z0-9]+")

# SWE-bench instance ids are ``<owner>__<repo>-<gold PR number>``; the trailing number
# is the PR that carries the gold fix, so it is the fix-PR to exclude from the window.
_FIX_PR = re.compile(r"-(\d+)$")


def fix_pr_number(instance: Instance) -> int | None:
    """The gold fix-PR number parsed from the instance id, or ``None`` if absent.

    SWE-bench ids end in ``-<PR number>`` (the PR the gold patch came from). Excluding
    it by number is a deterministic belt to the diff-restate suspenders: a squashed or
    rebased fix-PR whose lines no longer match the gold patch verbatim would slip past
    :func:`_restates_gold`, but its number never changes.
    """
    m = _FIX_PR.search(instance.instance_id)
    return int(m.group(1)) if m else None


def org_id_for(instance: Instance) -> str:
    """Deterministic per-instance org id, e.g. ``swebench_sympy__sympy_12345``.

    Sanitized to the ``[A-Za-z0-9_]`` set so it's a safe org identifier; the
    same instance always maps to the same org (idempotent create, R5 isolation).
    """
    safe = _SANITIZE.sub("_", instance.instance_id).strip("_")
    return f"swebench_{safe}"


def create_org(client: HttpClient, org_id: str) -> None:
    """``POST /orgs`` for ``org_id``; idempotent — a 409 (already exists) is swallowed."""
    try:
        client.post_orgs({"orgId": org_id, "name": None, "password": _ORG_PASSWORD})
    except OrgConflict:
        pass  # already created by a prior run — reuse it read-after-write


def _merged_pr_list(fetch: Fetcher, limit: int) -> list[dict]:
    """`gh pr list --state merged --json number,mergedAt,title` → list of dicts.

    Extends ``pr_source.list_merged_prs`` (numbers only) — we need ``mergedAt`` to
    date-filter the window.
    """
    raw = fetch(["gh", "pr", "list", "--state", "merged", "--limit", str(limit),
                 "--json", "number,mergedAt,title"])
    return json.loads(raw)


# Normalize a diff to its substantive +/- code lines (drop headers/hunk markers,
# strip whitespace) so a fix-restating PR is detected regardless of context noise.
_DIFF_NOISE = ("diff --git", "index ", "--- ", "+++ ", "@@", "rename ", "similarity ",
               "new file", "deleted file", "Binary files")


def _diff_changelines(diff: str) -> set[str]:
    out: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(_DIFF_NOISE):
            continue
        if line and line[0] in "+-":
            body = line[1:].strip()
            if body:
                out.add(body)
    return out


def _restates_gold(pr_diff: str, gold_patch: str) -> bool:
    """True iff ``pr_diff`` contains every substantive change line of ``gold_patch``.

    Compared on normalized +/- code lines (whitespace/context-insensitive). An
    empty gold patch never matches (can't restate nothing).
    """
    gold = _diff_changelines(gold_patch)
    if not gold:
        return False
    return gold <= _diff_changelines(pr_diff)


def select_window(instance: Instance, fetch: Fetcher,
                  *, limit: int = DEFAULT_LIST_LIMIT) -> list[int]:
    """PR numbers merged BEFORE the instance's cutoff, oldest-first, fix excluded.

    The cutoff is :attr:`Instance.created_at` — the instance's issue/commit date,
    the pragmatic point-in-time bound available offline (the base_commit's own date
    isn't on the record). A PR is in-window iff ``mergedAt < created_at``; a PR
    merged at/after the cutoff is excluded. The instance's own fix-PR and any PR
    whose diff restates the gold diff are dropped (see :func:`select_window`'s use
    in :func:`ingest_window`). Returns oldest-first so in-window contradictions
    resolve to latest-as-of-cutoff.

    The instance's fix-PR is excluded here by number (see :func:`fix_pr_number`).
    The fix-*restating* drop (a different PR that merely re-expresses the gold diff)
    needs each PR's diff, so it's applied in :func:`ingest_window` (which already
    fetches diffs); this function does the date-window + ordering + fix-number drop.
    """
    cutoff = instance.created_at
    fix = fix_pr_number(instance)
    rows = _merged_pr_list(fetch, limit)
    in_window = [
        r for r in rows
        if r.get("mergedAt") and str(r["mergedAt"]) < cutoff and int(r["number"]) != fix
    ]
    # Oldest-first: ascending mergedAt, number as a stable tiebreak.
    in_window.sort(key=lambda r: (str(r.get("mergedAt", "")), int(r["number"])))
    return [int(r["number"]) for r in in_window]


def ingest_window(instance: Instance, client: HttpClient, fetch: Fetcher,
                  *, limit: int = DEFAULT_LIST_LIMIT) -> IngestResult:
    """Ingest the selected window oldest-first; drop any fix-restating PR.

    For each in-window PR (oldest-first): build its document, drop it if its diff
    restates the gold diff, else ``POST /ingest`` (``state="active"``,
    ``source="git/pr:<n>"``) scoped to the instance's org via ``X-Praxis-Org``.
    """
    org_id = org_id_for(instance)
    candidates = select_window(instance, fetch, limit=limit)

    ingested_numbers: list[int] = []
    actions: list[str] = []
    facts_total = 0
    for n in candidates:
        doc = build_pr_document(n, fetch=fetch)
        if _restates_gold(doc.diff, instance.patch):
            continue  # fix-restating PR — excluded so the snapshot can't leak the answer
        res = client.post_ingest(org_id, {
            "documents": [{"text": doc.render(), "source": f"git/pr:{n}"}],
            "state": "active",
            "onConflict": "auto_resolve",
        })
        result0 = res["results"][0]
        actions.append(str(result0.get("action", "")))
        facts_total += int(result0.get("facts", 0) or 0)
        ingested_numbers.append(n)

    return IngestResult(
        org_id=org_id,
        pr_numbers=ingested_numbers,
        ingested=len(ingested_numbers),
        ingestion_cost=None,  # /ingest surfaces counts, not cost; see IngestResult docstring
        facts_ingested=facts_total,
        actions=actions,
    )


class LeakageError(AssertionError):
    """An ingested fact restates the gold diff — the snapshot leaked the answer."""


def leakage_guard(instance: Instance, client: HttpClient, *, top_k: int = 8) -> None:
    """Raise loudly if any retrievable fact restates the gold diff.

    Queries ``GET /context`` over the instance's org using the gold-changed file
    paths + issue text, and raises :class:`LeakageError` if any returned fact's text
    contains a substantive gold change line. Otherwise returns ``None`` (passes).
    """
    org_id = org_id_for(instance)
    query = " ".join(instance.gold_files) + " " + instance.problem_statement
    ctx = client.get_context(org_id, query.strip(), top_k)
    gold = _diff_changelines(instance.patch)
    if not gold:
        return
    for hit in ctx.get("hits", []):
        text = str(hit.get("text", ""))
        for line in gold:
            if line in text:
                raise LeakageError(
                    f"ingested fact restates gold diff for {instance.instance_id}: "
                    f"fact={hit.get('id')!r} matched gold line {line[:60]!r}"
                )


def run_ingest(instance: Instance, *, client: HttpClient, fetch: Fetcher,
               limit: int = DEFAULT_LIST_LIMIT) -> IngestResult:
    """create_org → ingest_window → leakage_guard for one instance.

    The single entry point a caller (U6 orchestration) uses per instance.
    """
    org_id = org_id_for(instance)
    create_org(client, org_id)
    result = ingest_window(instance, client, fetch, limit=limit)
    leakage_guard(instance, client)
    return result
