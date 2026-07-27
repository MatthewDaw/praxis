"""Box-level integration of a finished job branch into the repo's main worktree (R33).

All work merges into the repo's main worktree, and the outbound publish (the ref update to the
remote plus opening the pull request) happens only from there — never from a job worktree. The
reset-to-PR-base, merge, remote-ref update, and pull-request opening are one sequence, serialized
per repo under :class:`RepoIntegrationLock` held for the WHOLE sequence (never released and
reacquired mid-sequence), so two same-repo jobs finishing at once never interleave their sequences
against the one shared main worktree.

Integration refuses — never resets — a main worktree that is dirty or holds a commit not yet
reflected upstream at the intended PR base, since a reset would silently discard real work; the
refusal records ``needs-attention`` on the job (``box_service_failures``) and leaves the main
worktree untouched. A conflicting merge is aborted before raising, so a caller's
``MERGE_CONFLICT`` failure record always corresponds to a main worktree left exactly where it was
before the attempt.

The remote-ref update and pull-request opening are guarded by the shared, reusable
``box_service_push_guard`` core (target-repo / namespace / force / existing-ref refusal) before
either is attempted, and both route their outbound call through argv lists rather than a shell
string, so no literal command text for either lands in the source tree outside this module and its
tests.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from knowledge.serve.box_service_delivery import DeliveryAction, reconcile_delivery
from knowledge.serve.box_service_failures import FailureClass, record_failure
from knowledge.serve.box_service_models import DeliveryStage, Job
from knowledge.serve.box_service_push_guard import PushRequest, evaluate_push

#: Same shape as ``subprocess.run`` — the seam a fake runner replaces in tests.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Clock = Callable[[], float]
#: A pull-request-creation seam: given the target and the merged sha, returns the created PR's URL.
PrCreator = Callable[["IntegrationTarget", str], str]
#: An existing-pull-request-lookup seam (R62): given the target, returns the URL of an already-open
#: pull request for ``target.integration_ref``, or ``None`` if there is none — the re-detection
#: replay uses instead of trusting a durable stage blindly.
ExistingPrLookup = Callable[["IntegrationTarget"], "str | None"]

#: Default staleness window for the per-repo integration lock, mirroring the shape (not the value)
#: of every other lease in the system (holder id, heartbeat, expiry).
DEFAULT_LOCK_TTL_S = 300.0


class IntegrationError(RuntimeError):
    """Base class for a refused or failed integration. Never silently swallowed (R17: refuse
    rather than degrade)."""


class IntegrationLockedError(IntegrationError):
    """A different, live holder already has this repo's integration in flight; reset/merge/
    publish sequences for the same repo never interleave."""


class MainWorktreeDirtyError(IntegrationError):
    """Refuse to reset a main worktree that has uncommitted changes or a commit not yet reflected
    upstream at the PR base — resetting would silently discard real work."""


class MergeConflictError(IntegrationError):
    """The job branch conflicts merging into the main worktree (R34). The branch is preserved and
    the main worktree is left exactly where it was before the merge attempt — never partially
    merged."""


class PublishRefusedError(IntegrationError):
    """The push guard refused the outbound ref update — wrong target repo, ref outside the
    reserved integration namespace, a force update, or an already-existing remote ref."""


class UnreconcilableDeliveryStageError(IntegrationError):
    """R62: on replay, the job's durable delivery stage does not reconcile with the re-detected
    remote state (e.g. it claims the pull request is being opened but the published branch is
    missing). Never guessed or retried blind — the job branch is left completely untouched and the
    job is recorded ``needs-attention`` (``FailureClass.DELIVERY_STAGE_UNRECONCILABLE``)."""


@dataclass(frozen=True)
class IntegrationTarget:
    """Everything one integration sequence needs, resolved ahead of time so this module owns no
    lookup of its own — just the main worktree it must run in (never a job worktree).

    ``origin_repo`` and ``allowlisted_origin`` are deliberately SEPARATE fields (R36): the former is
    whatever repo this sequence is actually about to push to, the latter is the job's own
    independently-registered origin. A caller that resolves ``origin_repo`` from anywhere other
    than the job's trusted record (a bug, a stale cache, a misrouted job) is refused by
    ``evaluate_push`` rather than silently passing a same-field-compared-to-itself check that could
    never catch a real divergence.
    """

    main_worktree_path: str
    origin_repo: str
    allowlisted_origin: str
    job_branch: str
    pr_base: str
    integration_ref: str


@dataclass(frozen=True)
class IntegrationResult:
    merged_sha: str
    pushed_ref: str
    pr_url: str


@dataclass
class _LockEntry:
    holder_id: str
    heartbeat_at: float
    ttl: float


class RepoIntegrationLock:
    """An advisory, per-repo lock so two same-repo jobs finishing at once never interleave their
    reset/merge/publish sequences against the one shared main worktree.

    Carries a holder id, heartbeat, and expiry, like every other lease in the system: a stale
    entry (no heartbeat within its ``ttl``) is reclaimable by a new holder rather than stranding
    the repo forever behind a dead holder.
    """

    def __init__(self, *, clock: Clock = time.monotonic, ttl: float = DEFAULT_LOCK_TTL_S) -> None:
        self._clock = clock
        self._ttl = ttl
        self._entries: dict[str, _LockEntry] = {}

    def _is_stale(self, entry: _LockEntry) -> bool:
        return (self._clock() - entry.heartbeat_at) > entry.ttl

    def acquire(self, repo_key: str, holder_id: str) -> bool:
        """Take the lock for ``repo_key``. ``True`` iff ``holder_id`` now holds it — because it
        already did, the lock was free, or the previous holder's entry is stale. ``False`` iff a
        different, live holder has it."""
        entry = self._entries.get(repo_key)
        if entry is not None and entry.holder_id != holder_id and not self._is_stale(entry):
            return False
        self._entries[repo_key] = _LockEntry(
            holder_id=holder_id, heartbeat_at=self._clock(), ttl=self._ttl
        )
        return True

    def heartbeat(self, repo_key: str, holder_id: str) -> bool:
        entry = self._entries.get(repo_key)
        if entry is None or entry.holder_id != holder_id:
            return False
        entry.heartbeat_at = self._clock()
        return True

    def release(self, repo_key: str, holder_id: str) -> bool:
        """Give up the lock; ``False`` iff ``holder_id`` does not currently hold ``repo_key`` (a
        no-op, never raises)."""
        entry = self._entries.get(repo_key)
        if entry is None or entry.holder_id != holder_id:
            return False
        del self._entries[repo_key]
        return True

    def held_by(self, repo_key: str) -> str | None:
        entry = self._entries.get(repo_key)
        if entry is None or self._is_stale(entry):
            return None
        return entry.holder_id


def run_git(runner: Runner, cwd: str, *args: str) -> str:
    """Run one git command against ``cwd``, raising :class:`IntegrationError` on a non-zero
    exit. Public (not module-private) because ``box_service_group_integrate`` reuses it for the
    group sequence's own git calls, rather than re-implementing this same wrap-and-raise shape."""
    proc = runner(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise IntegrationError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


#: The single line written into the main worktree's (untracked) ``.git/info/attributes``, which
#: gitattributes(5) gives HIGHER precedence than any tracked ``.gitattributes`` for the same path:
#: unsetting ``merge`` and ``filter`` for every path means a job branch's own ``.gitattributes``
#: can name a custom merge driver or clean/smudge filter all it wants — git falls back to a plain
#: content merge and leaves working-tree content unfiltered regardless, so neither ever runs.
_NEUTRALIZE_ATTRIBUTES_LINE = "* -merge -filter"


def _harden_main_worktree(runner: Runner, cwd: str) -> None:
    """Disable every session-authored code path a job branch's content could trigger during
    integration (R58): pin ``core.hooksPath`` to a location with no hooks (``os.devnull`` is never
    a directory a hook script can live under, so any hook — including one a job branch supplies
    under an existing tracked hooks directory — is silently skipped), and neutralize any custom
    merge driver / clean-smudge filter the branch's ``.gitattributes`` might name via the
    higher-precedence ``.git/info/attributes`` override below.

    Idempotent and cheap, so it runs at the top of every integration sequence (via
    :func:`reset_main_worktree_to_pr_base`) before anything is fetched, reset, or merged. Only
    touches the real filesystem when ``cwd`` is an actual directory — a scripted/fake runner used
    by other tests may pass a synthetic path that never exists on disk, and there is nothing to
    harden there.
    """
    run_git(runner, cwd, "config", "core.hooksPath", os.devnull)

    if not os.path.isdir(cwd):
        return
    info_dir = Path(cwd) / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    attrs_path = info_dir / "attributes"
    existing = attrs_path.read_text() if attrs_path.exists() else ""
    if _NEUTRALIZE_ATTRIBUTES_LINE not in existing.splitlines():
        with attrs_path.open("a") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(_NEUTRALIZE_ATTRIBUTES_LINE + "\n")


def reset_main_worktree_to_pr_base(
    runner: Runner, cwd: str, pr_base: str, *, job: Job | None = None
) -> None:
    """The shared R33 preflight: refuse (:class:`MainWorktreeDirtyError`) a main worktree that is
    dirty or holds a commit not yet reflected upstream at ``pr_base``, recording
    ``FailureClass.MAIN_WORKTREE_DIRTY`` on ``job`` when given; otherwise fetch the PR base and
    hard-reset ``cwd`` to it. Shared by :func:`run_integration_sequence` (single job) and
    ``box_service_group_integrate.run_group_integration_sequence`` (group), since both start every
    integration sequence from the identical resolved-PR-base state.

    Also the single shared point (R58) where every job-branch-influenceable code path — repo hooks,
    ``.gitattributes`` merge drivers, clean/smudge filters — is disabled for the whole sequence
    before any fetch, reset, or merge runs; see :func:`_harden_main_worktree`.
    """
    _harden_main_worktree(runner, cwd)

    status = run_git(runner, cwd, "status", "--porcelain")
    if status.strip():
        if job is not None:
            record_failure(job, FailureClass.MAIN_WORKTREE_DIRTY)
        raise MainWorktreeDirtyError(
            f"main worktree {cwd!r} has uncommitted changes, refusing to reset"
        )

    tracking_ref = f"refs/remotes/origin/{pr_base}"
    run_git(runner, cwd, "fetch", "origin", f"+{pr_base}:{tracking_ref}")

    unpushed = run_git(runner, cwd, "log", f"{tracking_ref}..HEAD", "--oneline")
    if unpushed.strip():
        if job is not None:
            record_failure(job, FailureClass.MAIN_WORKTREE_DIRTY)
        raise MainWorktreeDirtyError(
            f"main worktree {cwd!r} holds a commit not yet reflected at origin/{pr_base}, "
            "refusing to reset"
        )

    run_git(runner, cwd, "reset", "--hard", tracking_ref)


def default_pr_creator(target: IntegrationTarget, merged_sha: str) -> str:
    """The default pull-request-creation seam: the GitHub CLI's PR subcommand, invoked as
    separate argv elements (never a single shell string) from the main worktree only."""
    args = [
        "gh", "pr", "create",
        "--base", target.pr_base,
        "--head", target.integration_ref,
        "--title", f"Integrate {target.job_branch}",
        "--body", f"Merged {merged_sha}",
    ]
    proc = subprocess.run(args, cwd=target.main_worktree_path, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise IntegrationError(f"pull-request creation failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def run_integration_sequence(
    target: IntegrationTarget,
    *,
    holder_id: str,
    lock: RepoIntegrationLock,
    runner: Runner = subprocess.run,
    pr_creator: PrCreator = default_pr_creator,
    force_publish: bool = False,
    remote_ref_exists: bool = False,
    job: Job | None = None,
) -> IntegrationResult:
    """Run the box service's ONE reset -> merge -> remote-ref-update -> pull-request-opening
    sequence for a finished job branch, entirely from ``target.main_worktree_path`` and never a
    job worktree (R33).

    Serialized per repo via ``lock`` HELD FOR THE WHOLE SEQUENCE (acquired once, released once in
    ``finally``) so two same-repo jobs finishing simultaneously never interleave. Raises
    :class:`IntegrationLockedError` if a different, live holder already has this repo's
    integration in flight.

    Refuses (:class:`MainWorktreeDirtyError`) rather than resetting a main worktree that has
    uncommitted changes or a commit not yet reflected upstream at ``target.pr_base`` — an
    operator's work must never be silently discarded by a reset. When ``job`` is given, the
    refusal is also recorded as ``FailureClass.MAIN_WORKTREE_DIRTY`` (needs-attention), and the
    main worktree is left completely untouched (no reset attempted).

    On merge conflict, aborts the merge, records ``FailureClass.MERGE_CONFLICT`` on ``job`` (when
    given), and raises :class:`MergeConflictError` preserving ``target.job_branch`` with the main
    worktree exactly where it was before the attempt (R34).

    The remote-ref update and pull-request creation are each checked against
    ``box_service_push_guard.evaluate_push`` first; a refusal raises
    :class:`PublishRefusedError` and neither the remote ref nor a pull request is touched.
    """
    repo_key = target.main_worktree_path
    if not lock.acquire(repo_key, holder_id):
        raise IntegrationLockedError(
            f"integration for {repo_key!r} is already in flight under a different holder"
        )
    try:
        cwd = target.main_worktree_path

        reset_main_worktree_to_pr_base(runner, cwd, target.pr_base, job=job)

        merge_proc = runner(
            ["git", "merge", "--no-ff", target.job_branch],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
        if merge_proc.returncode != 0:
            runner(["git", "merge", "--abort"], cwd=cwd, capture_output=True, text=True, check=False)
            if job is not None:
                record_failure(job, FailureClass.MERGE_CONFLICT)
            raise MergeConflictError(
                f"merge of {target.job_branch!r} into {cwd!r} conflicted, job branch preserved"
            )

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

        if job is not None:
            job.delivery_stage = DeliveryStage.PUBLISHING
        run_git(runner, cwd, "push", "origin", f"HEAD:{target.integration_ref}")

        if job is not None:
            job.delivery_stage = DeliveryStage.OPENING_PR
        pr_url = pr_creator(target, merged_sha)

        if job is not None:
            job.delivery_stage = DeliveryStage.DELIVERED
        return IntegrationResult(merged_sha=merged_sha, pushed_ref=target.integration_ref, pr_url=pr_url)
    finally:
        lock.release(repo_key, holder_id)


def replay_integration_sequence(
    target: IntegrationTarget,
    *,
    holder_id: str,
    lock: RepoIntegrationLock,
    job: Job,
    runner: Runner = subprocess.run,
    pr_creator: PrCreator = default_pr_creator,
    existing_pr_lookup: ExistingPrLookup,
    force_publish: bool = False,
    remote_ref_exists: bool = False,
) -> IntegrationResult:
    """Replay ``target``'s integration sequence after a crash (R62), using ``job.delivery_stage``
    — recorded BEFORE each irreversible step by :func:`run_integration_sequence` — to decide what
    is safe to do next, RE-DETECTING the real remote state (``remote_ref_exists``,
    ``existing_pr_lookup``) rather than trusting the recorded stage blindly.

    Given a crash between the push and the pull-request creation (stage ``PUBLISHING`` with the
    remote ref already published, or stage ``OPENING_PR``), replay opens exactly one pull request
    for the already-existing branch — it never pushes again, and it reuses an already-open pull
    request instead of opening a second one. Given nothing published yet (stage ``NOT_STARTED``,
    or ``PUBLISHING`` with no remote ref found), replay safely runs the ordinary full sequence.

    Given a recorded stage that does not reconcile with the re-detected remote state, replay raises
    :class:`UnreconcilableDeliveryStageError`, records ``FailureClass.DELIVERY_STAGE_UNRECONCILABLE``
    on ``job`` (needs-attention), and touches no git state at all — the job branch is left exactly
    as it was, never retried blind.
    """
    decision = reconcile_delivery(
        job.delivery_stage,
        remote_ref_exists=remote_ref_exists,
        existing_pr_url=existing_pr_lookup(target),
    )

    if decision.action is DeliveryAction.NEEDS_ATTENTION:
        record_failure(job, FailureClass.DELIVERY_STAGE_UNRECONCILABLE)
        raise UnreconcilableDeliveryStageError(
            decision.reason or "delivery stage does not reconcile with the remote state"
        )

    if decision.action in (DeliveryAction.REUSE_EXISTING_PR, DeliveryAction.ALREADY_DELIVERED):
        assert decision.pr_url is not None
        job.delivery_stage = DeliveryStage.DELIVERED
        return IntegrationResult(
            merged_sha="", pushed_ref=target.integration_ref, pr_url=decision.pr_url
        )

    if decision.action is DeliveryAction.RUN_FULL_SEQUENCE:
        job.delivery_stage = DeliveryStage.NOT_STARTED
        return run_integration_sequence(
            target,
            holder_id=holder_id,
            lock=lock,
            runner=runner,
            pr_creator=pr_creator,
            force_publish=force_publish,
            remote_ref_exists=False,
            job=job,
        )

    # DeliveryAction.SKIP_PUSH_OPEN_PR: the branch is already published (from a previous attempt,
    # run entirely from the main worktree per R33) — never push again, go straight to opening
    # exactly one pull request.
    assert decision.action is DeliveryAction.SKIP_PUSH_OPEN_PR
    repo_key = target.main_worktree_path
    if not lock.acquire(repo_key, holder_id):
        raise IntegrationLockedError(
            f"integration for {repo_key!r} is already in flight under a different holder"
        )
    try:
        cwd = target.main_worktree_path
        merged_sha = run_git(runner, cwd, "rev-parse", "HEAD").strip()

        job.delivery_stage = DeliveryStage.OPENING_PR
        pr_url = pr_creator(target, merged_sha)

        job.delivery_stage = DeliveryStage.DELIVERED
        return IntegrationResult(merged_sha=merged_sha, pushed_ref=target.integration_ref, pr_url=pr_url)
    finally:
        lock.release(repo_key, holder_id)
