"""One command that answers "how is the campaign going".

Written because that question was asked repeatedly during the first real campaign and every
answer required hand-rolling a script against the space file over ssh. A status that is
laborious to obtain is a status nobody checks, and the two worst failures of that campaign --
a stage silently wedged, and two runs racing on one idea -- were both things a routine glance
would have caught immediately.

Reads only. It never mutates the space, so it is safe to run against a live campaign.
"""

from __future__ import annotations

from typing import Any

from knowledge.ml_registry.schema import IDEA, MODEL, TERMINAL_TRIAL_STATUSES, TRIAL
from knowledge.ml_registry.write_path import RegistrySpace

#: Verdict statuses an idea can carry, ordered so a report reads worst-to-best consistently.
IDEA_STATUSES = ("adopted", "rejected", "parked", "voided", "superseded")


def campaign_status(space: RegistrySpace, model_id: str) -> dict[str, Any]:
    """Everything worth knowing about a campaign, in one read-only pass.

    Surfaces the two conditions that are otherwise invisible: an IN-FLIGHT trial (which blocks
    its idea and means a run is either alive or died without resolving), and the ratchet count
    (which silently approaches a rollback of the last adoption).
    """
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise KeyError(f"model {model_id!r} was never registered")

    ideas = [f for f in space.list_facts(IDEA) if f.meta.get("model_id") == model_id]
    trials = [f for f in space.list_facts(TRIAL) if f.meta.get("model_id") == model_id]
    in_flight = [t for t in trials
                 if str(t.meta.get("status", "")) not in TERMINAL_TRIAL_STATUSES]

    by_status: dict[str, list[str]] = {}
    for idea in ideas:
        st = str(idea.meta.get("status") or "untried")
        by_status.setdefault(st, []).append(str(idea.meta.get("id") or idea.id))

    return {
        "model_id": model_id,
        "metric": model.meta.get("metric"),
        "direction": model.meta.get("direction"),
        "baseline": model.meta.get("baseline"),
        "previous_baseline": model.meta.get("previous_baseline"),
        "noise_floor": model.meta.get("noise_floor"),
        # HOW WIDE the bar is in sigmas, WHY, and whether anything ever checked that the
        # floor is really that many sigmas. A campaign running a loose bar is a fact about
        # how to read every verdict it produces, so it belongs in the routine glance.
        "sigmas": model.meta.get("sigmas"),
        "sigmas_reason": model.meta.get("sigmas_reason"),
        "sigmas_basis": model.meta.get("sigmas_basis"),
        "baseline_throughput": model.meta.get("baseline_throughput"),
        "void_throughput_fraction": model.meta.get("void_throughput_fraction", 0.05),
        "ideas_total": len(ideas),
        "ideas_by_status": {k: sorted(v) for k, v in sorted(by_status.items())},
        "trials_total": len(trials),
        # A trial in flight blocks its idea. If no process is running, that run died without
        # resolving and the idea is wedged until it is superseded.
        "trials_in_flight": [{"trial_id": t.id, "idea_id": t.meta.get("idea_id"),
                              "commit": t.meta.get("commit"),
                              "status": t.meta.get("status")} for t in in_flight],
        # Approaches a rollback of the last adoption silently. Worth seeing before it fires.
        "ratchet_count": model.meta.get("ratchet_count", 0),
        "rejection_streak_ideas": list(model.meta.get("rejection_streak_ideas") or []),
        "ratchet_resets": list(model.meta.get("ratchet_resets") or []),
        "diagnoses": diagnose(space, model_id),
    }


def _sigmas_note(status: dict[str, Any]) -> str:
    """How many sigmas the floor is, and -- when nothing could check that -- say so.

    An unverifiable claim rendered exactly like a verified one is the shape of the original
    defect: court-marking's record said `sigmas: 2` beside a one-sigma floor, and every reader
    of that record, human or otherwise, took the 2 at face value.
    """
    sigmas = status.get("sigmas")
    if sigmas in (None, ""):
        return ""
    note = f"  sigmas={sigmas}"
    if status.get("sigmas_basis") == "unverified_external_measurement":
        note += " (UNVERIFIED: floor measured outside praxis)"
    if status.get("sigmas_reason"):
        note += f"  [{status['sigmas_reason']}]"
    return note


def format_status(status: dict[str, Any]) -> str:
    """Human-readable rendering. The JSON is for programs; this is for the question being asked."""
    lines = [
        f"model      {status['model_id']}  metric={status['metric']} ({status['direction']})",
        f"baseline   {status['baseline']}  floor={status['noise_floor']}"
        f"{_sigmas_note(status)}"
        f"  void_ref={status['baseline_throughput']}"
        f"  speed_void={status.get('void_throughput_fraction', 0.05)}",
        f"ideas      {status['ideas_total']} total, {status['trials_total']} trial(s) run",
    ]
    for st in list(IDEA_STATUSES) + ["untried"]:
        ids = status["ideas_by_status"].get(st)
        if ids:
            lines.append(f"  {st:<11} {len(ids):3d}  {', '.join(ids)}")

    if status["trials_in_flight"]:
        lines.append("")
        lines.append("IN FLIGHT (blocks its idea; if nothing is running, this run died):")
        for t in status["trials_in_flight"]:
            lines.append(f"  {t['trial_id']}  idea={t['idea_id']}  commit={t['commit']}")

    for d in status.get("diagnoses", []):
        lines.append("")
        lines.append(f"{d['severity'].upper()}: {d['kind']}")
        lines.append(f"  {d['detail']}")

    ratchet = status["ratchet_count"] or 0
    if ratchet:
        lines.append("")
        lines.append(f"RATCHET    {ratchet}/3 -- at 3 the last adoption is rolled back"
                     f"  ({', '.join(status['rejection_streak_ideas'])})")
    if status["ratchet_resets"]:
        lines.append(f"  cleared {len(status['ratchet_resets'])} time(s) at stage boundaries")
    return "\n".join(lines)

#: Below this the bar is looser than the conservative two-sigma one, which is a legitimate
#: explore-bias choice and NEVER refused -- but it changes how every verdict should be read.
CONSERVATIVE_SIGMAS = 2.0
#: How many UNTRIED ideas make a loose bar worth mentioning. The concern is multiplicity: one
#: arm at one sigma is fine, a queue of them manufactures winners. Ten is where the expected
#: false adoptions (0.159 each) cross one whole arm.
LOOSE_BAR_BACKLOG_THRESHOLD = 10
#: One-sided probability that a NULL arm clears the bar, measured on the detection campaign.
_FALSE_ADOPTION_RATE = {1.0: 0.159, 2.0: 0.023}


#: Two voids of the same KIND is not bad luck, it is a mis-set harness. One is noise; the second
#: says the setting that produced it will keep producing it, and a loop that merely re-runs will
#: reproduce the same truncation until CLOSE_VOID_LIMIT ends the campaign with no explanation.
REPEATED_VOID_THRESHOLD = 2


def _loose_bar_advisory(space: RegistrySpace, model_id: str) -> list[dict[str, str]]:
    """WARN -- never refuse -- when a sub-two-sigma bar meets a large untried backlog.

    This is the precise condition the old two-sigma default existed to prevent: at one sigma a
    null arm clears the bar 15.9% of the time one-sided (2.3% at two sigma), so ~10 expected
    false adoptions over a 66-idea backlog rather than ~1.5, and a false adoption is the
    expensive direction -- it raises the baseline permanently and composes into every later arm,
    while a false rejection costs one re-run.

    REFUSING would be wrong. One sigma is now the standing default and a deliberate
    explore-bias trade: a two-sigma bar over a noisy metric is one nothing can clear, and
    detection ran 34 trials and adopted nothing under exactly that. SILENCE would also be
    wrong -- it is how court-marking ran an entire campaign at half the band its own record
    claimed. So it is said out loud, at INFO, in the one report an operator actually reads.

    It lives HERE rather than at registration because the backlog is what makes it true, and at
    registration there is no backlog: ideas are registered after the model, so a
    registration-time version of this warning would fire on an empty queue every time and be
    tuned out before the first real one.
    """
    model = space.get(model_id)
    if model is None:
        return []
    sigmas = model.meta.get("sigmas")
    if sigmas in (None, ""):
        return []
    try:
        sigmas_f = float(sigmas)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []
    if sigmas_f >= CONSERVATIVE_SIGMAS:
        return []
    untried = [f for f in space.list_facts(IDEA)
               if f.meta.get("model_id") == model_id
               and str(f.meta.get("status") or "untried") == "untried"]
    if len(untried) < LOOSE_BAR_BACKLOG_THRESHOLD:
        return []
    rate = _FALSE_ADOPTION_RATE.get(sigmas_f, _FALSE_ADOPTION_RATE[1.0])
    tight = _FALSE_ADOPTION_RATE[CONSERVATIVE_SIGMAS]
    reason = model.meta.get("sigmas_reason")
    return [{
        "kind": "loose_bar_with_large_backlog",
        "severity": "info",
        "detail": (
            f"noise_floor is {sigmas_f} sigma with {len(untried)} untried idea(s) queued. A NULL "
            f"arm clears a {sigmas_f}-sigma bar about {rate:.1%} of the time one-sided, against "
            f"{tight:.1%} at {CONSERVATIVE_SIGMAS} sigma -- roughly "
            f"{len(untried) * rate:.1f} expected false adoptions across this backlog rather than "
            f"{len(untried) * tight:.1f}. This is NOT an error and nothing is blocked: a loose "
            "bar is a legitimate explore-bias choice, and a bar nothing can clear buys no safety "
            "(detection ran 34 trials at 2 sigma and adopted nothing). But the two errors are "
            "asymmetric -- a false rejection costs one re-run, a false adoption raises the "
            "baseline permanently and composes into every later arm -- and the RATCHET is the "
            "only thing that unwinds one. It counts a rejection only when the trial would have "
            "parked-or-won against the PREVIOUS baseline, which is deliberately conservative and "
            "will not reliably unwind a stack of small false adoptions. Read every adoption in "
            "this campaign with that in mind, or set sigmas: 2."
            + (f" Declared reason: {reason}" if reason else
               " No sigmas_reason is recorded; declare one so the choice outlives whoever made it.")
        ),
    }]


def diagnose(space: RegistrySpace, model_id: str) -> list[dict[str, str]]:
    """Actionable diagnoses a supervising loop can act on WITHOUT a human reading the ledger.

    A void is a decision to re-run. That is correct once, and wrong the moment the reason is
    something re-running cannot change. The registry already knows the difference -- it recorded
    `void_reason` -- but nothing turned that into advice, so an autonomous loop would re-run a
    truncated arm, truncate it again, and void again until the campaign closed on the void limit
    having explained nothing.

    Observed on the first campaign to run expensive arms: the two most costly architectures in the
    backlog -- a graph model and a widened recurrent one -- both exceeded a wall clock tuned for
    heads that finish in a third of the time. Both were voided. Re-running either would have
    reproduced the truncation exactly. That is a selection effect, not a measurement: a budget
    tuned to cheap heads silently removes precisely the arms that might beat them, and the
    campaign converges on "cheap models win" as an artefact of its own harness.
    """
    trials = [f for f in space.list_facts(TRIAL) if f.meta.get("model_id") == model_id]
    voided = [t for t in trials if str(t.meta.get("status")) == "voided"]

    unfair = [t for t in voided if "not a fair run" in str(t.meta.get("void_reason", ""))]
    slow = [t for t in voided if "throughput" in str(t.meta.get("void_reason", ""))]

    model = space.get(model_id)
    acks = {a["kind"]: a for a in (model.meta.get(ACKNOWLEDGED_FIELD) or [])} if model else {}
    counts = {"budget_too_small": len(unfair), "void_gate_too_tight": len(slow)}

    def _suppressed(kind: str) -> bool:
        # Suppressed only while NO NEW void of this kind has appeared since the acknowledgement.
        ack = acks.get(kind)
        return ack is not None and counts.get(kind, 0) <= int(ack.get("void_count_at_ack", 0))

    out: list[dict[str, str]] = []
    out.extend(_loose_bar_advisory(space, model_id))
    if len(unfair) >= REPEATED_VOID_THRESHOLD and not _suppressed("budget_too_small"):
        out.append({
            "kind": "budget_too_small",
            "severity": "blocking",
            "detail": f"{len(unfair)} arms voided as unfair runs (truncated). RE-RUNNING WILL NOT "
                      f"HELP if the budget is the cause -- the same wall clock truncates them "
                      f"again. Raise the run budget above the SLOWEST arm you intend to try, not "
                      f"the typical one; a budget tuned to cheap arms silently removes the "
                      f"expensive ones and the campaign converges on 'cheap wins' as an artefact "
                      f"of its own harness. BUT FIRST CHECK THE MACHINE IS NOT SHARED: a "
                      f"wall-clock budget measures the neighbours as much as the arm, and "
                      f"throughput -- which also gates VOID -- is wall-clock derived, so a busy "
                      f"co-tenant can void arms that are perfectly healthy. On a shared host, "
                      f"budget on CPU TIME (resource.getrusage) rather than wall clock, or the "
                      f"same arm passes and fails depending on who else is running.",
        })
    if len(slow) >= REPEATED_VOID_THRESHOLD and not _suppressed("void_gate_too_tight"):
        out.append({
            "kind": "void_gate_too_tight",
            "severity": "blocking",
            "detail": f"{len(slow)} arms voided for throughput. If those arms are structurally "
                      f"slower (a richer representation, a heavier head) they can NEVER pass, and "
                      f"the gate is rejecting them on cost rather than merit. Set "
                      f"void_throughput_fraction to 0 to disable it, or reference it against the "
                      f"SLOWEST baseline run rather than the median.",
        })

    # An idea whose most recent trial voided is waiting to be re-run. Nothing else says so, and a
    # loop that treats voided as answered leaves it permanently unmeasured -- a non-answer that
    # records nothing, which is strictly worse than a rejection.
    latest: dict[str, object] = {}
    for t in trials:
        latest[str(t.meta.get("idea_id"))] = t
    needs_rerun = sorted(iid for iid, t in latest.items()
                         if str(t.meta.get("status")) == "voided")
    if needs_rerun:
        out.append({
            "kind": "awaiting_rerun",
            "severity": "info",
            "detail": f"{len(needs_rerun)} idea(s) whose latest trial voided and are still "
                      f"unmeasured: {', '.join(needs_rerun)}",
        })
    return out

#: Diagnoses the operator has acknowledged as REMEDIATED, with the void count at the time. A
#: diagnosis is computed from history, so it survives the fix that resolved it -- and an
#: autonomous loop that stops on a blocking diagnosis then stops FOREVER.
ACKNOWLEDGED_FIELD = "acknowledged_diagnoses"


def acknowledge_diagnosis(space: RegistrySpace, model_id: str, kind: str,
                          reason: str) -> dict[str, object]:
    """Record that a blocking diagnosis has been REMEDIATED, so the loop may proceed.

    Diagnoses are computed from the trial history, which means they outlive their own cause. A
    budget raised, a gate disabled, a machine moved -- none of that erases the voids that prompted
    the diagnosis, so it keeps firing and a loop that halts on it halts permanently.

    Observed immediately: two arms voided under a wall-clock budget on a shared box. The budget
    was changed to CPU time, which fixes the cause -- and the loop still refused to dispatch,
    because two voided trials were still sitting in the history.

    This is NOT a mute. The void count of that kind is recorded at acknowledgement, and the
    diagnosis fires again the moment a NEW void of the same kind appears. Acknowledging a cause
    you did not actually fix therefore buys exactly one more arm before the loop stops again --
    which is the right cost, because it makes a false acknowledgement cheap to detect and
    impossible to sustain.

    ``reason`` is required and recorded: an acknowledgement without one is indistinguishable from
    silencing an inconvenient blocker.
    """
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise KeyError(f"model {model_id!r} was never registered")
    if not reason or not reason.strip():
        raise ValueError("an acknowledgement reason is required")

    counts = _void_counts(space, model_id)
    acks = list(model.meta.get(ACKNOWLEDGED_FIELD) or [])
    acks = [a for a in acks if a.get("kind") != kind]          # supersede any earlier ack
    acks.append({"kind": kind, "reason": reason, "void_count_at_ack": counts.get(kind, 0)})
    model.meta[ACKNOWLEDGED_FIELD] = acks
    return {"kind": kind, "void_count_at_ack": counts.get(kind, 0)}


def _void_counts(space: RegistrySpace, model_id: str) -> dict[str, int]:
    """How many voids of each diagnosable kind exist right now."""
    trials = [f for f in space.list_facts(TRIAL) if f.meta.get("model_id") == model_id]
    voided = [t for t in trials if str(t.meta.get("status")) == "voided"]
    return {
        "budget_too_small": sum(1 for t in voided
                                if "not a fair run" in str(t.meta.get("void_reason", ""))),
        "void_gate_too_tight": sum(1 for t in voided
                                   if "throughput" in str(t.meta.get("void_reason", ""))),
    }

#: Trial statuses that mean the idea's question was ANSWERED by that run.
#:
#: `voided` is absent deliberately: a voided run was unfair, so the question is still open and the
#: arm must be re-run. `complete`/`running` are absent because the trial is still IN FLIGHT --
#: `complete` means the training finished and is awaiting adjudication, not that a verdict exists.
ANSWERING_TRIAL_STATUSES = frozenset({"succeeded", "failed", "stagnant", "superseded"})


def idea_verdicts(space: RegistrySpace, model_id: str, *,
                  statuses: frozenset[str] | None = ANSWERING_TRIAL_STATUSES) -> dict[str, str]:
    """``{idea tag: trial status}`` for ideas whose LATEST trial answered them.

    Derived from trials, not from `idea.meta.status`, and the distinction caused a live incident.
    `resolve-verdict` writes the verdict onto the TRIAL; it does not stamp the idea. A campaign
    that asked the idea instead saw `None` for every arm the registry had just adjudicated, decided
    those arms were unanswered, deleted their local verdicts and re-queued them -- running the same
    arms again, and again, indefinitely. It re-ran three arms before it was caught, and nothing in
    the loop could notice: every iteration looked like honest new work.

    Keyed by the project's own tag (`meta["id"]`) because that is what a backlog is written in.

    Pass ``statuses=None`` for EVERY latest-trial status, including `voided` and `complete`. A
    caller that must distinguish "voided, so re-run it" from "never attempted" needs that: with
    the default filter both appear simply as absent, and a loop that reads absence as "re-run"
    will re-run arms it has already settled. That conflation is what produced the infinite
    re-queue this function was written to fix, so leaving it possible would only move the bug.
    """
    ideas = {f.id: str(f.meta.get("id") or f.id) for f in space.list_facts(IDEA)
             if f.meta.get("model_id") == model_id}
    latest: dict[str, Any] = {}
    for t in space.list_facts(TRIAL):
        if t.meta.get("model_id") == model_id and str(t.meta.get("idea_id")) in ideas:
            latest[str(t.meta.get("idea_id"))] = t          # later registrations win
    # An explicit REOPEN outranks a stale trial. reopen-idea returns an idea to untried because
    # its verdict was never fairly earned, but the trial that produced that verdict still sits in
    # the history saying `failed` or `stagnant` -- so a trial-derived answer silently re-answers
    # the question the reopen just opened, and the arm is never re-run.
    #
    # Measured: seven arms were reopened after being measured on a superseded architecture, and
    # every one of them still read as answered. The reopen printed OK and changed nothing.
    # No idea-level exclusion. reopen_idea VOIDS the prior trials instead, which is precise and
    # self-limiting: a trial registered after the reopen answers normally, so an idea that was
    # reopened, re-run and adjudicated is answered again. Excluding the idea itself made that
    # impossible -- it stayed unanswered forever and the arm re-ran on every iteration.
    return {ideas[i]: str(t.meta.get("status"))
            for i, t in latest.items()
            if statuses is None or str(t.meta.get("status")) in statuses}
