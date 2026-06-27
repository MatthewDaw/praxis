"""U3: per-instance *space* ingest — a point-in-time Praxis snapshot for one instance.

WHY a *space per instance* (within one fixed ``swebench_eval`` org): the space becomes
a point-in-time snapshot holding only PRs merged before the instance's ``base_commit``.
Ingesting **oldest-first** means any in-window contradictions resolve to the
latest-as-of-``base_commit`` value (the stock ingest path's ``auto_resolve`` keeps the
newest writer). The instance's fix-PR — and any PR whose diff merely restates the gold
diff — is **excluded**, and a leakage guard fails loudly if any ingested fact restates
the gold diff, so the snapshot can never leak the answer into the treatment arm.
Spaces isolate instances from each other (R5): each ingest posts only its own PRs under
its own ``X-Praxis-Space`` (effective tenant ``dev-user::space:<id>``). A stable space id
also makes a **rerun reuse** the prior snapshot instead of re-distilling. Spaces are the
right primitive — the eval is one tenant running many isolated working graphs, not many
tenants — and far lighter than the org-per-instance it replaces.

Two seams keep the whole pipeline offline-testable, mirroring ``pr_source.py``'s
injected ``Fetcher``:

* :data:`Fetcher` — the argv→stdout ``gh``/``git`` callable. We wrap
  :func:`knowledge.injestion.pr_source.default_fetcher` with
  :func:`make_repo_fetcher` so PRs are fetched from ``sympy/sympy`` (``gh -R``),
  not the cwd. Tests inject a fake that switches on argv and returns fixture JSON.
* :class:`HttpClient` — a tiny injectable client carrying the fixed eval ``org`` and
  threading the per-instance ``space`` on ``POST /spaces`` / ``POST /ingest`` /
  ``GET /context`` / ``GET /graph``. The default :class:`UrllibClient` uses ``urllib``
  (no extra dependency); tests inject a fake. **No real HTTP in tests.**

The cutoff date is :attr:`Instance.created_at` (the instance's issue/commit date) —
the pragmatic point-in-time bound available offline; see :func:`select_window`.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from knowledge.injestion.pr_source import (
    Fetcher,
    build_pr_document,
    default_fetcher,
)
from knowledge.evals.swebench.instances import Instance, is_substantive_line

# Default dev backend (PRAXIS_AUTH_DISABLED=1 → no login needed); the org owner is
# the dev tenant `dev-user`, who is the auto-member that `active_org` authorizes.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# One fixed org holds the whole eval; per-instance isolation rides on SPACES (named
# private working graphs within the org), not a fresh org per instance. A space is the
# right primitive here — the eval is one tenant running many isolated point-in-time
# graphs, exactly "different agents drive distinct live graphs concurrently". The
# effective tenant user becomes `dev-user::space:<space_id>`, so facts never cross-bleed
# between instances (R5) while staying far lighter than an org-per-instance.
EVAL_ORG = "swebench_eval"

# Dev-tenant org password (auth is disabled in dev; the value is required but unused
# for membership since the creator is the auto-member).
_ORG_PASSWORD = "swebench-eval"

# Size of the pre-base_commit window: the N most-recent PRs merged before the cutoff
# (the date-bounded `gh` search returns newest-first, so `limit` directly sizes the
# window). The brainstorm's intent was the 30-50 most recent PRs; each PR is one
# server-side LLM distillation, so this is also the per-instance ingestion-cost knob.
DEFAULT_LIST_LIMIT = 50


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
    """The backend calls U3 needs; a fake implements the same shape offline.

    The client carries the fixed eval ``org`` (sent as ``X-Praxis-Org`` on every call);
    per-instance isolation is the ``space`` argument (sent as ``X-Praxis-Space``).
    """

    org: str

    def post_orgs(self, body: dict) -> dict: ...

    def post_spaces(self, space_id: str, name: str | None = None) -> dict: ...

    def post_ingest(self, space: str, body: dict) -> dict: ...

    def get_context(self, space: str, query: str, top_k: int) -> dict: ...

    def get_graph(self, space: str, state: str = "active") -> dict: ...


# Transient-failure retry policy for UrllibClient (mirrors openrouter_http.default_post):
# 429 (slowapi per-route limit / OpenRouter upstream) and 5xx are flaky, not fatal — retry
# with exponential backoff so a higher --ingest-workers backs off instead of crashing.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 0.5  # seconds; doubles each retry (0.5, 1.0, 2.0)


class OrgConflict(Exception):
    """``POST /orgs`` returned 409 — the org already exists (idempotent create)."""


class SpaceConflict(Exception):
    """``POST /spaces`` returned 409 — the space already exists (idempotent create)."""


@dataclass
class UrllibClient:
    """Default :class:`HttpClient` over ``urllib`` (mirrors the smoke driver).

    ``org`` is the fixed eval org sent on every request; ``space`` (per call) selects the
    instance's private working graph via ``X-Praxis-Space``.
    """

    base_url: str = DEFAULT_BASE_URL
    org: str = EVAL_ORG

    def _call(self, method: str, path: str, *, space: str | None = None,
              body: dict | None = None, params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "X-Praxis-Org": self.org}
        if space:
            headers["X-Praxis-Space"] = space

        # Retry transient failures with exponential backoff (mirrors the LLM client's
        # openrouter_http.default_post). The paid routes (/ingest, /context) are rate-
        # limited to 30/min and the distillation behind them can 429 upstream from
        # OpenRouter — both are transient, so a higher --ingest-workers degrades into
        # backoff instead of crashing. A 429 from slowapi or OpenRouter is in the
        # retryable set; 409 (org/space exists) is NOT, so it still propagates to
        # post_orgs/post_spaces to become OrgConflict/SpaceConflict, and other 4xx fail fast.
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRYABLE_STATUS:
                    raise  # 409 conflict / other 4xx — caller handles or fails fast
                last_exc = exc
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc  # connection reset / timeout — transient
            if attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(_BACKOFF_BASE * 2**attempt)
        assert last_exc is not None  # only reached after a caught failure
        raise last_exc

    def post_orgs(self, body: dict) -> dict:
        try:
            return self._call("POST", "/orgs", body=body)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise OrgConflict(body.get("orgId", "")) from exc
            raise

    def post_spaces(self, space_id: str, name: str | None = None) -> dict:
        try:
            return self._call("POST", "/spaces", body={"spaceId": space_id, "name": name})
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise SpaceConflict(space_id) from exc
            raise

    def post_ingest(self, space: str, body: dict) -> dict:
        return self._call("POST", "/ingest", space=space, body=body)

    def get_context(self, space: str, query: str, top_k: int) -> dict:
        return self._call("GET", "/context", space=space,
                          params={"query": query, "top_k": str(top_k)})

    def get_graph(self, space: str, state: str = "active") -> dict:
        return self._call("GET", "/graph", space=space, params={"state": state})


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

    space_id: str
    pr_numbers: list[int]
    ingested: int  # documents POSTed (one per selected PR)
    reused: bool = False  # space already populated → ingest skipped (rerun reuse)
    ingestion_cost: float | None = None
    facts_ingested: int = 0
    actions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline functions.
# ---------------------------------------------------------------------------
# A space_id is a slug: lowercase letters/digits/dash/underscore (the backend's
# `_SPACE_SLUG_RE`). SWE-bench ids (`sympy__sympy-27904`) already qualify, so the slug is
# human-readable — it names the repo + gold-PR. Anything out of set collapses to '-'.
_SANITIZE = re.compile(r"[^a-z0-9_-]+")

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


def space_id_for(instance: Instance) -> str:
    """Deterministic, human-readable per-instance space id (e.g. ``sympy__sympy-27904``).

    Slugified from the instance id (lowercased, out-of-set chars → '-'); the same instance
    always maps to the same space, so a **rerun reuses that space** (idempotent create +
    populated-skip) rather than re-ingesting. R5 isolation holds: each space is a distinct
    ``dev-user::space:<id>`` tenant graph.
    """
    return _SANITIZE.sub("-", instance.instance_id.lower()).strip("-")


def ensure_eval_org(client: HttpClient) -> None:
    """``POST /orgs`` for the one fixed eval org; idempotent — a 409 is swallowed.

    Spaces live inside an org and ``active_org`` proves membership, so the eval needs
    exactly one org the dev tenant owns. Created once; every instance's space lives here.
    """
    try:
        client.post_orgs({"orgId": client.org, "name": "swebench eval", "password": _ORG_PASSWORD})
    except OrgConflict:
        pass  # already created by a prior run


def ensure_space(client: HttpClient, space_id: str) -> None:
    """``POST /spaces`` for ``space_id`` in the eval org; idempotent — a 409 is swallowed."""
    try:
        client.post_spaces(space_id)
    except SpaceConflict:
        pass  # already created by a prior run — reuse it


def space_is_populated(client: HttpClient, space_id: str) -> int:
    """Active-fact count in ``space_id`` (the rerun-reuse signal; 0 ⇒ ingest needed).

    Reads ``GET /graph`` for the space and counts active nodes. A populated space means a
    prior run already ingested this instance's window, so the rerun reuses it as-is.
    """
    graph = client.get_graph(space_id, state="active").get("graph") or {}
    return len(graph.get("nodes") or [])


def _parse_ts(value: str):
    """Parse a timestamp to a UTC-aware ``datetime``, tolerant of the two real formats.

    SWE-rebench's ``created_at`` is naive space-separated (``2015-10-19 13:52:59``);
    ``gh``'s ``mergedAt`` is RFC3339 (``2026-06-25T18:59:44Z``). A lexical compare of the
    raw strings mis-orders them whenever the date matches but the format differs (``'T'``
    sorts after ``' '``), so we parse both and normalize a naive value to UTC. Returns
    ``None`` when the value is empty/unparseable (such a PR is then treated as outside
    the window — we don't ingest something we can't place in time)."""
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _merged_pr_list(fetch: Fetcher, limit: int, before_date: str | None = None) -> list[dict]:
    """`gh pr list --state merged [--search "merged:<=DATE"] --json number,mergedAt,title`.

    Extends ``pr_source.list_merged_prs`` (numbers only) — we need ``mergedAt`` to
    date-filter the window. CRITICAL: ``gh pr list`` returns the ``limit`` *most recent*
    merges, so without a date bound an instance whose ``base_commit`` predates the last
    ``limit`` merges gets an EMPTY window (all recent merges post-date the cutoff). We
    bound the query with GitHub search ``merged:<=<date>`` so it returns PRs merged up to
    and including the cutoff day, newest-first — the actual pre-``base_commit`` window.
    The day is inclusive here (date-granular search); :func:`select_window`'s precise
    ``_parse_ts`` filter then drops any same-day PR merged after the exact cutoff time.
    """
    argv = ["gh", "pr", "list", "--state", "merged", "--limit", str(limit),
            "--json", "number,mergedAt,title"]
    if before_date:
        argv += ["--search", f"merged:<={before_date}"]
    return json.loads(fetch(argv))


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
    # gh's `mergedAt` (RFC3339, Z) and SWE-rebench's `created_at` (naive, space-separated)
    # are DIFFERENT formats, so we parse both to UTC datetimes rather than compare strings.
    cutoff = _parse_ts(instance.created_at)
    fix = fix_pr_number(instance)
    # Bound the gh query by merge date so it reaches the pre-base_commit window (not just
    # the latest `limit` merges). The date portion of created_at is the search granularity.
    before_date = instance.created_at[:10] if instance.created_at else None
    rows = _merged_pr_list(fetch, limit, before_date)
    dated = [(r, _parse_ts(r.get("mergedAt", ""))) for r in rows]
    in_window = [
        r for r, merged in dated
        if cutoff is not None and merged is not None
        and merged < cutoff and int(r["number"]) != fix
    ]
    # Oldest-first: ascending mergedAt, number as a stable tiebreak.
    in_window.sort(key=lambda r: (_parse_ts(r.get("mergedAt", "")), int(r["number"])))
    return [int(r["number"]) for r in in_window]


# Mirrors the server's POST /ingest body cap (knowledge.serve.app._MAX_BODY_BYTES): the
# handler 413s when the *summed document-text bytes* exceed this. Batching documents up to
# this packs the most PRs per request — and since the per-route limit is "30/minute" of
# REQUESTS, one post of N docs costs the same rate budget as one of 1. Fewer requests is the
# real ingest-throughput lever (not more workers — see run.py / README).
_MAX_INGEST_BYTES = 128 * 1024


def _batches(docs: list[tuple[int, str]], *, max_bytes: int):
    """Yield consecutive runs of ``(number, text)`` whose summed UTF-8 text bytes stay within
    ``max_bytes`` — the metric the server's body cap uses, so matching it maximizes batching
    without tripping the 413. Order is preserved (within and across batches). A single
    document larger than ``max_bytes`` is yielded alone (it will 413 — same outcome as the
    old one-doc-per-call path, never silently dropped)."""
    batch: list[tuple[int, str]] = []
    size = 0
    for n, text in docs:
        b = len(text.encode("utf-8"))
        if batch and size + b > max_bytes:
            yield batch
            batch, size = [], 0
        batch.append((n, text))
        size += b
    if batch:
        yield batch


def ingest_window(instance: Instance, client: HttpClient, fetch: Fetcher,
                  *, limit: int = DEFAULT_LIST_LIMIT) -> IngestResult:
    """Ingest the selected window oldest-first; drop any fix-restating PR.

    Builds each in-window PR's document oldest-first, dropping any whose diff restates the
    gold diff, then ``POST /ingest`` (``state="active"``, ``source="git/pr:<n>"``) scoped to
    the instance's space. PRs are **batched** into as few posts as the server body cap
    (``_MAX_INGEST_BYTES``) allows: the 30/min limit counts requests, so batching turns ~50
    rate-limited calls into 1–2. Oldest-first order is preserved within and across batches,
    so the handler's per-document ``auto_resolve`` still keeps the latest-as-of-cutoff writer.
    """
    space_id = space_id_for(instance)
    candidates = select_window(instance, fetch, limit=limit)

    # Build (number, rendered-text) oldest-first, dropping fix-restating PRs up front so they
    # never enter a batch (keeps the snapshot from leaking the gold answer).
    docs: list[tuple[int, str]] = []
    for n in candidates:
        doc = build_pr_document(n, fetch=fetch)
        if _restates_gold(doc.diff, instance.patch):
            continue
        docs.append((n, doc.render()))

    ingested_numbers: list[int] = []
    actions: list[str] = []
    facts_total = 0
    for batch in _batches(docs, max_bytes=_MAX_INGEST_BYTES):
        res = client.post_ingest(space_id, {
            "documents": [{"text": text, "source": f"git/pr:{n}"} for n, text in batch],
            "state": "active",
            "onConflict": "auto_resolve",
        })
        # The POST succeeded, so every doc in the batch was ingested; read per-doc facts from
        # the results array (one entry per document, in order), defensively if it's short.
        results = res.get("results") or []
        for i, (n, _text) in enumerate(batch):
            result = results[i] if i < len(results) else {}
            actions.append(str(result.get("action", "")))
            facts_total += int(result.get("facts", 0) or 0)
            ingested_numbers.append(n)

    return IngestResult(
        space_id=space_id,
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

    Queries ``GET /context`` over the instance's space using the gold-changed file
    paths + issue text, and raises :class:`LeakageError` if any returned fact's text
    contains a substantive gold change line. Otherwise returns ``None`` (passes).
    """
    space_id = space_id_for(instance)
    query = " ".join(instance.gold_files) + " " + instance.problem_statement
    ctx = client.get_context(space_id, query.strip(), top_k)
    # Only SUBSTANTIVE gold lines count: a bare ``return`` or other short/boilerplate line
    # appears in countless facts and would abort a run on a spurious match (it did, on
    # sympy-27797). Same predicate the selection-time screen uses, so the two agree.
    gold = {ln for ln in _diff_changelines(instance.patch) if is_substantive_line(ln)}
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
               limit: int = DEFAULT_LIST_LIMIT, reuse: bool = True) -> IngestResult:
    """ensure org+space → (reuse-skip OR ingest_window + leakage_guard) for one instance.

    The single entry point a caller (U6 orchestration) uses per instance. With
    ``reuse=True`` (default), a space already populated by a prior run is reused as-is —
    no re-ingest, no re-distillation — which is the whole point of a stable per-instance
    space id. ``reuse=False`` forces a fresh ingest into whatever the space already holds.
    """
    space_id = space_id_for(instance)
    ensure_eval_org(client)
    ensure_space(client, space_id)

    if reuse:
        existing = space_is_populated(client, space_id)
        if existing > 0:
            # Reuse the prior run's snapshot; still run the leakage guard (cheap, and it
            # re-asserts the no-leak invariant against the reused facts).
            leakage_guard(instance, client)
            return IngestResult(space_id=space_id, pr_numbers=[], ingested=0,
                                reused=True, facts_ingested=existing)

    result = ingest_window(instance, client, fetch, limit=limit)
    leakage_guard(instance, client)
    return result
