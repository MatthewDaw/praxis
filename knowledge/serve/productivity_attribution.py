"""Commit-to-owner attribution for the productivity series (R11).

Given the per-repository commit activity the GraphQL commit-activity client (R2/R3) returns —
a dict keyed by ``"owner/name"`` -> a list of commit dicts, each carrying at least
``additions``, ``author_login`` and optionally ``author_email`` — decides which commits count
toward the owner's S1 series (additions) versus which are unattributable.

A commit counts toward S1 when its GraphQL ``author.user.login`` equals the token owner's
login, falling back to a match against the owner's verified email addresses (``author_email``)
when no login is present. Commits resolving to neither are accumulated into
``unattributed_count`` instead of being silently dropped or misattributed to the owner.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


def _classify(
    commit: dict[str, Any], owner_login_norm: str, owner_emails_norm: set[str]
) -> str:
    """Classify one commit as ``"owned"``, ``"unattributed"`` or ``"other"``.

    Shared by :func:`attribute_commit_activity` (the whole-window S1 sum) and
    :func:`bucketed_owner_totals` (the per-bucket S1/S2 series) so the ownership
    rule — login match, falling back to a verified-email match, else
    unattributed — is defined exactly once.
    """
    login = (commit.get("author_login") or "").strip().lower()
    email = (commit.get("author_email") or "").strip().lower()

    if login:
        if owner_login_norm and login == owner_login_norm:
            return "owned"
        return "other"  # a different, resolvable author.
    if email and email in owner_emails_norm:
        return "owned"
    return "unattributed"


def attribute_commit_activity(
    commit_activity: dict[str, list[dict[str, Any]]],
    owner_login: str,
    owner_emails: Iterable[str] = (),
) -> dict[str, int]:
    """Attribute commits across all repos in ``commit_activity`` to ``owner_login``.

    Returns ``{"s1": <sum of the owner's additions>, "unattributed_count": <int>}``.

    A commit is the owner's when its ``author_login`` case-insensitively equals
    ``owner_login``. When a commit has no login (the author's git email isn't linked to any
    GitHub account), it instead counts as the owner's when its ``author_email``
    case-insensitively matches one of ``owner_emails`` — otherwise it increments
    ``unattributed_count``. A commit with a login belonging to a different, resolvable author
    is neither counted toward S1 nor unattributed.
    """
    owner_login_norm = owner_login.strip().lower() if owner_login else ""
    owner_emails_norm = {e.strip().lower() for e in owner_emails if e}

    s1 = 0
    unattributed_count = 0
    for commits in commit_activity.values():
        for commit in commits:
            outcome = _classify(commit, owner_login_norm, owner_emails_norm)
            if outcome == "owned":
                s1 += commit.get("additions") or 0
            elif outcome == "unattributed":
                unattributed_count += 1
            # else "other": a different, resolvable author — neither S1 nor unattributed.

    return {"s1": s1, "unattributed_count": unattributed_count}


def bucketed_owner_totals(
    commit_activity: dict[str, list[dict[str, Any]]],
    owner_login: str,
    bucket_starts: list[datetime],
    bucket_seconds: float,
    owner_emails: Iterable[str] = (),
) -> dict[str, list[int]]:
    """Per-bucket owner-attributed additions (S1) and deletions (S2).

    Uses the same ownership rule as :func:`attribute_commit_activity` (login match,
    falling back to verified email, else unattributed), sorting each owned commit into
    the half-open ``[start, start + bucket_seconds)`` window its ``committedDate`` falls
    in (first match wins, matching :func:`knowledge.serve.productivity_series.bucket_counts`).
    A commit with no parseable ``committedDate``, or one that falls outside every bucket,
    contributes to no bucket.
    """
    owner_login_norm = owner_login.strip().lower() if owner_login else ""
    owner_emails_norm = {e.strip().lower() for e in owner_emails if e}

    s1 = [0] * len(bucket_starts)
    s2 = [0] * len(bucket_starts)
    for commits in commit_activity.values():
        for commit in commits:
            if _classify(commit, owner_login_norm, owner_emails_norm) != "owned":
                continue
            raw = commit.get("committedDate")
            if not raw:
                continue
            try:
                committed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if committed.tzinfo is None:
                committed = committed.replace(tzinfo=timezone.utc)
            for i, start in enumerate(bucket_starts):
                if start <= committed < start + timedelta(seconds=bucket_seconds):
                    s1[i] += commit.get("additions") or 0
                    s2[i] += commit.get("deletions") or 0
                    break

    return {"s1": s1, "s2": s2}
