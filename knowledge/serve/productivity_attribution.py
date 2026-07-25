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

from typing import Any, Iterable


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
            login = (commit.get("author_login") or "").strip().lower()
            email = (commit.get("author_email") or "").strip().lower()

            if login:
                if owner_login_norm and login == owner_login_norm:
                    s1 += commit.get("additions") or 0
                # else: a different, resolvable author — neither S1 nor unattributed.
            elif email and email in owner_emails_norm:
                s1 += commit.get("additions") or 0
            else:
                unattributed_count += 1

    return {"s1": s1, "unattributed_count": unattributed_count}
