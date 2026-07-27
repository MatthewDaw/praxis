"""Box-level GROUP integration (R49): merge every member's branch into the repo's main worktree in
DISPATCH ORDER, producing exactly one commit and opening exactly one pull request — rather than
one merge/commit/PR per member.

Builds directly on R33's single-job primitives (:mod:`box_service_integrate`): the same per-repo
``RepoIntegrationLock`` held for the whole sequence, the same dirty/unpushed-main-worktree refusal
via :func:`reset_main_worktree_to_pr_base`, and the same push-guard-gated publish. R48's barrier
(``box_service_groups.plan_group_integration``) is what decides WHICH members to pass here, in
dispatch order — this module only performs the merge/commit/publish once that decision is made.

Each member branch is folded in with ``git merge --squash`` (stages the branch's changes into the
index without creating a commit), so after every member has been folded in a SINGLE ``git commit``
produces the one commit the acceptance condition requires — never a merge commit per member. A
conflicting member's squash-merge is undone with a hard reset back to the resolved PR base (nothing
has been committed yet, so this fully undoes every squash applied so far for earlier members too),
preserving every member branch and leaving the main worktree exactly where it was before the group
attempt — the group-level mirror of R34.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from knowledge.serve.box_service_failures import FailureClass, record_failure
from knowledge.serve.box_service_integrate import (
    IntegrationError,
    IntegrationLockedError,
    IntegrationResult,
    MergeConflictError,
    PublishRefusedError,
    RepoIntegrationLock,
    Runner,
    reset_main_worktree_to_pr_base,
    run_git,
)
from knowledge.serve.box_service_models import Job, mark_completed
from knowledge.serve.box_service_push_guard import PushRequest, evaluate_push


@dataclass(frozen=True)
class GroupIntegrationTarget:
    """Everything one GROUP integration sequence needs. ``member_branches`` is the DISPATCH-ORDER
    list of branch names to fold in — the same order ``box_service_groups.members_of_group``
    returns (and the caller's original dispatch order); this module never re-sorts it.

    ``allowlisted_origin`` mirrors ``box_service_integrate.IntegrationTarget`` (R36): a field
    independent of ``origin_repo`` so a real divergence between the resolved push target and the
    group's registered origin is refused rather than compared against itself.
    """

    main_worktree_path: str
    origin_repo: str
    allowlisted_origin: str
    member_branches: list[str]
    pr_base: str
    integration_ref: str


#: A pull-request-creation seam for a group: given the target and the merged sha, returns the
#: created PR's URL — the group-sized analogue of ``box_service_integrate.PrCreator``.
GroupPrCreator = Callable[[GroupIntegrationTarget, str], str]


def default_group_pr_creator(target: GroupIntegrationTarget, merged_sha: str) -> str:
    """The default pull-request-creation seam for a group: ONE PR naming every merged member
    branch, invoked as separate argv elements (never a single shell string) from the main worktree
    only — mirrors ``box_service_integrate.default_pr_creator``."""
    args = [
        "gh", "pr", "create",
        "--base", target.pr_base,
        "--head", target.integration_ref,
        "--title", f"Integrate group ({len(target.member_branches)} members)",
        "--body", "Merged: " + ", ".join(target.member_branches) + f"\n\nMerged {merged_sha}",
    ]
    proc = subprocess.run(args, cwd=target.main_worktree_path, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise IntegrationError(f"pull-request creation failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def run_group_integration_sequence(
    target: GroupIntegrationTarget,
    *,
    holder_id: str,
    lock: RepoIntegrationLock,
    runner: Runner = subprocess.run,
    pr_creator: GroupPrCreator = default_group_pr_creator,
    force_publish: bool = False,
    remote_ref_exists: bool = False,
    jobs: list[Job] | None = None,
) -> IntegrationResult:
    """Run the box service's ONE reset -> (squash-merge every member, in dispatch order) -> ONE
    commit -> remote-ref-update -> ONE-pull-request-opening sequence for a group, entirely from
    ``target.main_worktree_path`` and never a job worktree (R49).

    Serialized per repo via ``lock`` HELD FOR THE WHOLE SEQUENCE, exactly like the single-job
    sequence, so a group's integration never interleaves with another same-repo integration
    (group or solo).

    ``jobs``, when given, must be positioned in the SAME dispatch order as
    ``target.member_branches`` (one job per branch) so a conflicting member's branch can be
    resolved back to its own ``Job`` and recorded as ``FailureClass.MERGE_CONFLICT`` — leaving
    every other member (including ones already folded in earlier in the loop) untouched, since
    nothing is committed until every member has merged cleanly. On success every member job is
    marked ``COMPLETED`` carrying its own branch and the ONE opened pull request's URL (R80).
    """
    repo_key = target.main_worktree_path
    if not lock.acquire(repo_key, holder_id):
        raise IntegrationLockedError(
            f"integration for {repo_key!r} is already in flight under a different holder"
        )
    try:
        cwd = target.main_worktree_path
        reset_main_worktree_to_pr_base(runner, cwd, target.pr_base)

        for index, branch in enumerate(target.member_branches):
            merge_proc = runner(
                ["git", "merge", "--squash", branch],
                cwd=cwd, capture_output=True, text=True, check=False,
            )
            if merge_proc.returncode != 0:
                # Nothing has been committed yet, so a hard reset to the resolved PR base fully
                # undoes this member's squash AND every earlier member's — the main worktree ends
                # up exactly where it started, and every member branch (including this one) is
                # preserved untouched.
                run_git(runner, cwd, "reset", "--hard", f"refs/remotes/origin/{target.pr_base}")
                if jobs is not None and index < len(jobs):
                    output = (merge_proc.stderr or merge_proc.stdout or "").strip()
                    record_failure(jobs[index], FailureClass.MERGE_CONFLICT, command_output=output)
                raise MergeConflictError(
                    f"merge of {branch!r} into {cwd!r} conflicted, every member branch preserved"
                )

        run_git(runner, cwd, "commit", "-m", f"Integrate group ({len(target.member_branches)} members)")
        merged_sha = run_git(runner, cwd, "rev-parse", "HEAD").strip()

        decision = evaluate_push(
            PushRequest(
                target_repo=target.origin_repo,
                ref=target.integration_ref,
                force=force_publish,
                remote_ref_exists=remote_ref_exists,
            ),
            allowlisted_origin=target.allowlisted_origin,
        )
        if not decision.allowed:
            raise PublishRefusedError(f"publish refused: {decision.reason}")

        run_git(runner, cwd, "push", "origin", f"HEAD:{target.integration_ref}")
        pr_url = pr_creator(target, merged_sha)

        if jobs is not None:
            for member_job, branch in zip(jobs, target.member_branches):
                mark_completed(member_job, branch=branch, pr_url=pr_url)

        return IntegrationResult(merged_sha=merged_sha, pushed_ref=target.integration_ref, pr_url=pr_url)
    finally:
        lock.release(repo_key, holder_id)
