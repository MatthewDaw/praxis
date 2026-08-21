"""Can this campaign actually RUN, right now, on THIS machine?

WHY THIS EXISTS. af-ml-campaign-loop.sh answers "is the campaign finished?" and nothing answers
"could it start?". Measured: the three live campaigns are byte-identical in git on the laptop and
on the devbox, and only one of them can run in either place -- the corpora are staged per machine,
outside git, and a campaign whose registry is perfectly green refuses at the first arm because the
pixels are not there. A queue runner that dispatches on registry state alone burns a slot to find
that out, and reports the refusal as a dispatch failure.

So every check here is a check a queue runner can act on BEFORE spending a slot, and the two that
matter most are the two that differ by machine and are invisible to git:

  CORPUS    -- asked by calling each lab's OWN resolver (det_lab.regime, contact_lab.paths,
               assoc_lab.corpus) in the lab's OWN interpreter. Never reimplemented here: two
               definitions of "present" drift, and the one that drifts is always the cheap copy.
               The lab's refusal message is reported VERBATIM, because it names the sync command.
  DISPATCH  -- IMPLEMENTED arms beside UNTRIED backlog ideas. That ratio, not the green ticks, is
               the readiness signal: measured today it is 4 arms against 56 ideas (detection),
               3 against 40 (contact_point), and for association 6 toggles that are all MEASURED
               NON-MOVES against 56 ideas. A campaign with zero un-run implemented arms is not
               ready to run unattended however green the rest of this output looks, because a
               shell loop cannot author an arm.

Everything here is READ-ONLY. campaign-status and campaign-complete never mutate a space, the
ledger is read with open(), and the corpus probes are the labs' own resolvers, which load.

OUTPUT CONTRACT (see also the header of af-ml-campaign-preflight.sh):

  stdout, one line per check, for programs:
      PREFLIGHT <campaign> <CHECK> <PASS|FAIL|INFO> k=v k=v ... [detail=<free text, last>]
      PREFLIGHT <campaign> RESULT <READY|NOT_READY|REFUSED> exit=<n> pass=<n> fail=<n> ...
      PREFLIGHT ALL RESULT <READY|NOT_READY|REFUSED> exit=<n> campaigns=<n> ready=<n>
  stderr, the human summary. A caller that wants only the contract reads stdout and drops stderr.

  Keys never contain spaces. `detail=` is always the LAST key on its line and may contain spaces;
  newlines inside it are escaped to a literal backslash-n so one check is always one line.

  EXIT CODES, and they are deliberately distinguishable:
      0  READY            -- every check passed and there is at least one un-run implemented arm.
      1  NOT_READY        -- at least one check FAILED.
      2  usage error.
      3  REFUSED          -- the campaign must not be supervised at all (C1 court-marking), or is
                            not a campaign this tool knows.
      4  NO_RUNNABLE_ARMS -- every check passed and there is NOTHING LEFT TO DISPATCH. Green, and
                            still not something to hand a loop. Separate from 1 so a queue can
                            tell "broken" from "needs an agent to author work".
  With --all the exit code is the WORST over the campaigns, ordered 0 < 4 < 1 < 3.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

READY, NOT_READY, USAGE, REFUSED, NO_RUNNABLE_ARMS = 0, 1, 2, 3, 4
_SEVERITY = {READY: 0, NO_RUNNABLE_ARMS: 1, NOT_READY: 2, REFUSED: 3}

#: How long any single lab probe may take. assoc_lab.corpus.load_eval parses 28,494 annotated
#: frames; det_lab.regime.eval_frames stats 429 paths. Neither decodes a pixel, so a probe that
#: has not returned in five minutes is wedged, not slow.
PROBE_TIMEOUT_S = 300


@dataclass(frozen=True)
class Campaign:
    """One live campaign, and the three questions only its own code can answer."""

    name: str
    space: str                 # relative to the sports_analysis repo
    model_id: str
    ledger: str
    #: Python run in the LAB's interpreter. Prints "OK <detail>" or raises; the raised message is
    #: reported verbatim. This is the lab's own resolver, never a reimplementation of it.
    corpus_probe: str
    #: Prints "ARMS <comma-separated>" from whatever the lab calls its arm table.
    arms_probe: str
    #: The module af-ml-campaign-loop.sh would drive, or "" when the campaign has none.
    composing_module: str
    dispatch: str
    #: Implemented arms already MEASURED as no-ops. They are implemented and un-run by the ledger,
    #: and dispatching one still buys nothing, so they must not be counted as runnable work.
    known_nonmove_arms: tuple[str, ...] = ()
    nonmove_evidence: str = ""


@dataclass(frozen=True)
class PreflightManifest:
    """Versioned project input consumed by the generic preflight engine."""

    schema_version: int
    campaigns: dict[str, Campaign]
    refused: dict[str, str]


def load_manifest(path: Path) -> PreflightManifest:
    """Load a version-1 preflight manifest without importing project code."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read preflight manifest {path}: {exc}") from exc
    if payload.get("schema_version") != 1:
        raise ValueError("preflight manifest schema_version must be 1")
    raw_campaigns = payload.get("campaigns")
    if not isinstance(raw_campaigns, list):
        raise ValueError("preflight manifest campaigns must be a list")
    campaigns: dict[str, Campaign] = {}
    required = {
        "name", "space", "model_id", "ledger", "corpus_probe", "arms_probe",
        "composing_module", "dispatch",
    }
    for index, raw in enumerate(raw_campaigns):
        if not isinstance(raw, dict):
            raise ValueError(f"campaigns[{index}] must be an object")
        missing = sorted(required - raw.keys())
        if missing:
            raise ValueError(f"campaigns[{index}] missing: {', '.join(missing)}")
        unknown = sorted(set(raw) - required - {"known_nonmove_arms", "nonmove_evidence"})
        if unknown:
            raise ValueError(f"campaigns[{index}] unknown keys: {', '.join(unknown)}")
        campaign = Campaign(
            **{key: raw[key] for key in required},
            known_nonmove_arms=tuple(raw.get("known_nonmove_arms", ())),
            nonmove_evidence=raw.get("nonmove_evidence", ""),
        )
        if campaign.name in campaigns:
            raise ValueError(f"duplicate campaign name: {campaign.name}")
        campaigns[campaign.name] = campaign
    raw_refused = payload.get("refused", {})
    if not isinstance(raw_refused, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw_refused.items()
    ):
        raise ValueError("preflight manifest refused must map names to reasons")
    return PreflightManifest(1, campaigns, dict(raw_refused))


CAMPAIGNS: dict[str, Campaign] = {
    "detection_shipped": Campaign(
        name="detection_shipped",
        space="ml/detection_shipped/registry/space.json",
        model_id="model-dcc0557b8454",
        ledger="ml/detection_shipped/results.tsv",
        corpus_probe=(
            "from det_lab.regime import eval_frames\n"
            "f = eval_frames()\n"
            "print('OK eval_frames=%d' % len(f))\n"
        ),
        arms_probe="from det_lab.campaign import ARMS; print('ARMS ' + ','.join(sorted(ARMS)))",
        composing_module="det_lab.campaign2",
        dispatch=("uv run python -m det_lab.campaign2 run --arm ARM --tag TAG "
                  "--base-tag incumbent"),
    ),
    "detection": Campaign(
        name="detection",
        space="ml/detection/registry/space.json",
        model_id="model-533010b57800",
        ledger="ml/detection/results.tsv",
        corpus_probe=(
            "from det_lab.regime import eval_frames\n"
            "f = eval_frames()\n"
            "print('OK eval_frames=%d' % len(f))\n"
        ),
        arms_probe="from det_lab.campaign import ARMS; print('ARMS ' + ','.join(sorted(ARMS)))",
        composing_module="det_lab.campaign",
        dispatch="uv run python -m det_lab.campaign run --arm ARM --tag TAG",
    ),
    "contact_point": Campaign(
        name="contact_point",
        space="ml/contact_point/registry/space.json",
        model_id="model-ebcad2dd3125",
        ledger="ml/contact_point/results.tsv",
        corpus_probe=(
            "import contact_lab.paths as p\n"
            "print('OK frames_dir=%s' % p.frames_dir())\n"
        ),
        arms_probe="from contact_lab.train import ARMS; print('ARMS ' + ','.join(sorted(ARMS)))",
        composing_module="contact_lab.campaign",
        dispatch="uv run python -m contact_lab.campaign --stage STAGE --max-arms 8",
    ),
    "association": Campaign(
        name="association",
        space="ml/association/registry/space.json",
        model_id="model-1d148ef55231",
        ledger="ml/association/results.tsv",
        corpus_probe=(
            "import assoc_lab.corpus as c\n"
            "s = c.load_eval()\n"
            "print('OK eval_sequences=%d frames=%d' % (len(s), sum(len(x.boxes) for x in s)))\n"
        ),
        arms_probe=(
            "from assoc_lab.arms import KNOWN_TOGGLES\n"
            "print('ARMS ' + ','.join(sorted(KNOWN_TOGGLES)))\n"
        ),
        composing_module="",   # there is none -- see the STRUCTURE check
        dispatch="(agent-authored, one arm at a time -- no composing module)",
        known_nonmove_arms=("buffer60", "buffer15", "buffer5", "buffer300", "cmc", "act30"),
        nonmove_evidence=(
            "every buffer value scores within 0.001 HOTA of the 0.851439 baseline; cmc is a "
            "literal no-op (ECC gets a zero frame, identity warp, the corpus carries no pixels); "
            "act30 is dead code, never passed to BotSort"
        ),
    ),
}

#: Named so the refusal is specific rather than a generic "unknown campaign". C1 is registered and
#: reachable, which is exactly why it needs a refusal: nothing else stops a queue picking it up.
FORBIDDEN = {
    "court_marking": "model-16659b6b8e45",
    "court-marking": "model-16659b6b8e45",
    "c1": "model-16659b6b8e45",
    "model-16659b6b8e45": "model-16659b6b8e45",
}


@dataclass
class Report:
    campaign: str
    lines: list[str] = field(default_factory=list)
    human: list[str] = field(default_factory=list)
    n_pass: int = 0
    n_fail: int = 0
    runnable_arms: int = -1

    def check(self, name: str, ok: bool | None, human: str, **kv: Any) -> None:
        """Record one check. ``ok=None`` is INFO: reported, never counted for or against."""
        state = "INFO" if ok is None else ("PASS" if ok else "FAIL")
        if ok is True:
            self.n_pass += 1
        elif ok is False:
            self.n_fail += 1
        detail = kv.pop("detail", None)
        parts = [f"{k}={_scrub(v)}" for k, v in kv.items()]
        if detail is not None:
            parts.append(f"detail={_scrub(detail, spaces_ok=True)}")
        self.lines.append(
            f"PREFLIGHT {self.campaign} {name} {state} " + " ".join(parts))
        self.human.append(f"  [{state:4}] {name:9} {human}")


def _scrub(value: Any, *, spaces_ok: bool = False) -> str:
    """One check is always one line, so newlines become literal escapes rather than a new row."""
    text = str(value).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "")
    return text if spaces_ok else text.replace(" ", "_")


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None,
         timeout: int = PROBE_TIMEOUT_S) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)
    return p.returncode, p.stdout, p.stderr


def _registry(praxis: Path, args: list[str]) -> tuple[int, str, str]:
    env = {**os.environ, "PRAXIS_DB_DISABLED": "1"}
    return _run(["uv", "run", "python", "-m", "knowledge.ml_registry.cli", *args], praxis, env)


def _lab_probe(repo: Path, code: str) -> tuple[int, str, str]:
    """Run ``code`` in the LAB's interpreter, not this one.

    The labs live in a different repo with a different virtualenv. Importing them from praxis's
    interpreter would either fail or -- far worse -- succeed against a stale copy on sys.path and
    report a corpus that the lab itself cannot reach.
    """
    return _run(["uv", "run", "python", "-c", code], repo)


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# --------------------------------------------------------------------------- checks

def _check_status(rep: Report, praxis: Path, space: Path, model_id: str) -> dict[str, Any] | None:
    code, out, err = _registry(praxis, ["campaign-status", "--space-file", str(space),
                                        "--model-id", model_id, "--json"])
    if code != 0:
        rep.check("STATUS", False, f"campaign-status failed (exit {code})",
                  exit=code, detail=_last_line(err) or _last_line(out))
        return None
    try:
        st = json.loads(out)
    except json.JSONDecodeError as exc:
        rep.check("STATUS", False, "campaign-status emitted unparseable JSON", detail=str(exc))
        return None
    untried = len(st["ideas_by_status"].get("untried", []))
    blocking = [d for d in st.get("diagnoses", []) if d.get("severity") == "blocking"]
    ok = not blocking
    rep.check(
        "STATUS", ok,
        f"baseline={st['baseline']} metric={st['metric']}({st['direction']}) "
        f"floor={st['noise_floor']} trials={st['trials_total']} untried={untried} "
        f"in_flight={len(st['trials_in_flight'])} blocking={len(blocking)}",
        baseline=st["baseline"], metric=st["metric"], direction=st["direction"],
        floor=st["noise_floor"], trials=st["trials_total"], untried=untried,
        ideas_total=st["ideas_total"], in_flight=len(st["trials_in_flight"]),
        ratchet=st.get("ratchet_count", 0), blocking=len(blocking),
        detail="; ".join(f"{d.get('kind')}: {d.get('detail')}" for d in blocking) or "none",
    )
    return st


def _check_complete(rep: Report, praxis: Path, space: Path, model_id: str,
                    stages: list[str], trials: int) -> None:
    """The reasons campaign-complete gives must have the shape the campaign's state implies.

    stage_thin is the one worth shouting about: a declared stage with fewer than 3 MEASURED arms
    will refuse to close, so the loop runs out its whole backlog and still cannot finish. Seeing
    it here costs a second; seeing it after a night of dispatching costs the night.
    """
    code, out, err = _registry(praxis, ["campaign-complete", "--space-file", str(space),
                                        "--model-id", model_id, "--stages", ",".join(stages),
                                        "--json"])
    if code not in (0, 1):
        rep.check("COMPLETE", False, f"campaign-complete errored (exit {code})",
                  exit=code, detail=_last_line(err) or _last_line(out))
        return
    try:
        res = json.loads(out)
    except json.JSONDecodeError as exc:
        rep.check("COMPLETE", False, "campaign-complete emitted unparseable JSON", detail=str(exc))
        return

    blocking = res.get("blocking", [])
    kinds = Counter(b["kind"] for b in blocking)
    thin = [b["stage"] for b in blocking if b["kind"] == "stage_thin"]
    never = [b["stage"] for b in blocking if b["kind"] == "stage_never_authored"]
    open_stages = {b["stage"] for b in blocking if b["kind"] == "stage_open"}

    problems = []
    if thin:
        problems.append("STAGE_THIN on " + ",".join(thin) +
                        " -- fewer than 3 measured arms, that stage will REFUSE TO CLOSE")
    if never:
        problems.append("stage_never_authored on " + ",".join(never))
    if trials == 0:
        # A campaign with no trials has an exactly predictable shape. Anything else means the
        # declared stages and the registered ideas disagree, which no amount of dispatching fixes.
        missing = [s for s in stages if s not in open_stages and s not in never]
        if missing:
            problems.append("0 trials but no stage_open for " + ",".join(missing))
        if "no_production_alias" not in kinds:
            problems.append("0 trials but no no_production_alias reason")

    rep.check(
        "COMPLETE", not problems,
        (f"done={res['done']} reasons=" +
         (",".join(f"{k}x{v}" for k, v in sorted(kinds.items())) or "none") +
         ("  " + " | ".join(problems) if problems else "  shape as expected")),
        done=res["done"], reasons=len(blocking),
        kinds=",".join(f"{k}:{v}" for k, v in sorted(kinds.items())) or "none",
        stage_thin=",".join(thin) or "none",
        detail=" | ".join(problems) or "expected shape for this trial count",
    )


def _check_ledger(rep: Report, ledger: Path, baseline_runs: list[str]) -> None:
    """A duplicate {sha}:{arm_tag} makes the whole ledger unreadable to praxis: adjudication joins
    a trial to its row by that key, and two rows for one key means there is no such thing as THE
    measured value of that arm."""
    if not ledger.is_file():
        rep.check("LEDGER", False, f"no ledger at {ledger}", path=ledger, rows=0)
        return
    with ledger.open(newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    keys = [r.get("commit", "") for r in rows]
    dupes = sorted(k for k, n in Counter(keys).items() if n > 1)
    missing = [b for b in baseline_runs if b not in set(keys)]
    ok = not dupes and not missing
    problems = []
    if dupes:
        problems.append("DUPLICATE join keys: " + ",".join(dupes))
    if missing:
        problems.append("baseline_runs absent from ledger: " + ",".join(missing))
    rep.check(
        "LEDGER", ok,
        f"rows={len(rows)} unique_keys={len(set(keys))} baseline_runs={len(baseline_runs)}"
        + ("  " + " | ".join(problems) if problems else "  keys unique, baselines all present"),
        path=ledger, rows=len(rows), unique_keys=len(set(keys)),
        duplicates=",".join(dupes) or "none",
        baseline_runs_missing=",".join(missing) or "none",
        detail=" | ".join(problems) or "ok",
    )


def _check_seed_drift(rep: Report, space: Path) -> None:
    """Identical to the tracked seed means 0 trials and nothing to lose. DIFFERING means live
    campaign state exists that is NOT in git, and a backup that only copies the repo loses it."""
    seed = space.with_name("space.seed.json")
    if not seed.is_file():
        rep.check("SEED", None, f"no space.seed.json beside {space.name} -- cannot compare",
                  seed="absent", differs="unknown")
        return
    live_bytes, seed_bytes = space.read_bytes(), seed.read_bytes()
    if live_bytes == seed_bytes:
        rep.check("SEED", None, "space.json is byte-identical to space.seed.json "
                  "(no live state beyond the seed)", seed="present", differs="false")
        return
    try:
        live_n = len(json.loads(live_bytes)["facts"])
        seed_n = len(json.loads(seed_bytes)["facts"])
        counts = f"facts {seed_n}(seed) -> {live_n}(live)"
    except Exception:
        counts = "facts uncountable"
    rep.check("SEED", None,
              f"space.json DIFFERS from space.seed.json ({counts}) -- untracked live state, "
              "the backup must cover it",
              seed="present", differs="true", detail=counts)


def _check_corpus(rep: Report, repo: Path, camp: Campaign) -> bool:
    code, out, err = _lab_probe(repo, camp.corpus_probe)
    if code == 0 and out.strip().startswith("OK"):
        rep.check("CORPUS", True, f"present on this machine -- {_last_line(out)[3:]} "
                  f"(via {camp.corpus_probe.splitlines()[0]})",
                  present="true", resolver=camp.name, detail=_last_line(out))
        return True
    # The lab's refusal names the sync command. Reproduce it verbatim rather than summarising it:
    # a summarised refusal is the one thing an operator cannot act on.
    refusal = _last_line(err) or _last_line(out) or f"probe exited {code} with no output"
    rep.check("CORPUS", False, "ABSENT on this machine. The lab's own resolver refuses:\n"
              f"           {refusal}",
              present="false", exit=code, detail=refusal)
    return False


def _check_dispatch(rep: Report, repo: Path, camp: Campaign, ledger: Path,
                    untried_ideas: int) -> None:
    """IMPLEMENTED arms beside UNTRIED ideas, because that ratio is the readiness signal."""
    code, out, err = _lab_probe(repo, camp.arms_probe)
    line = _last_line(out)
    if code != 0 or not line.startswith("ARMS "):
        rep.check("DISPATCH", False,
                  f"arm table not enumerable ({camp.arms_probe.splitlines()[0]})",
                  importable="false", exit=code, arms=0, untried_ideas=untried_ideas,
                  detail=_last_line(err) or line or f"probe exited {code}")
        rep.runnable_arms = 0
        return
    arms = [a for a in line[5:].split(",") if a]

    run_arms = _arms_seen_in_ledger(ledger, arms)
    nonmove = [a for a in arms if a in camp.known_nonmove_arms]
    runnable = [a for a in arms if a not in run_arms and a not in camp.known_nonmove_arms]
    rep.runnable_arms = len(runnable)

    verdict = (f"{len(arms)} implemented arm(s) vs {untried_ideas} untried backlog idea(s); "
               f"already in ledger: {','.join(sorted(run_arms)) or 'none'}; "
               f"known non-moves: {','.join(nonmove) or 'none'}; "
               f"UN-RUN AND WORTH RUNNING: {','.join(runnable) or 'NONE'}")
    if not runnable:
        verdict += ("\n           => NOT ready to run unattended: there is no implemented arm left "
                    "to dispatch. A shell loop cannot author one; this needs the af-ml-supervise "
                    "AGENT.")
    # PASS on ENUMERABLE, not on runnable>0. An empty arm table is not a broken campaign and must
    # not be reported as one: it is a campaign with nothing left to dispatch, which the caller
    # learns from exit 4 and runnable_arms=0. Folding the two into one FAIL would send a queue
    # looking for a bug that is not there.
    rep.check("DISPATCH", True, verdict,
              importable="true", arms=len(arms), arm_names=",".join(arms),
              untried_ideas=untried_ideas, arms_run=",".join(sorted(run_arms)) or "none",
              arms_nonmove=",".join(nonmove) or "none",
              arms_runnable=",".join(runnable) or "none",
              runnable_count=len(runnable),
              detail=camp.nonmove_evidence or "no known non-move arms")


def _arms_seen_in_ledger(ledger: Path, arms: list[str]) -> set[str]:
    """Which arms already have a measured row.

    Approximate ON PURPOSE, and it says so in the output: the three labs write the arm name into
    the ledger `description` in three different phrasings, and there is no arm column to join on.
    Substring match over description and join key over-counts rather than under-counts, so the
    error is always in the direction of reporting LESS runnable work -- which is the safe
    direction for a tool whose job is to stop a loop from being handed nothing to do.
    """
    if not ledger.is_file():
        return set()
    with ledger.open(newline="") as fh:
        blob = "\n".join(
            f"{r.get('commit', '')} {r.get('description', '')}" for r in csv.DictReader(fh, delimiter="\t"))
    return {a for a in arms if a in blob}


def _check_structure(rep: Report, repo: Path, camp: Campaign) -> None:
    """Whether af-ml-campaign-loop.sh can drive this campaign at all.

    Association has no campaign.py: src/assoc_lab holds arms.py, corpus.py, hota.py and train.py
    and nothing that composes a stage's worth of arms. The loop's AF_DISPATCH contract is "one
    command that runs ONE BATCH of arms", and no such command exists -- so association is
    agent-driven by design. Structural fact, reported as INFO, not a defect to fix.
    """
    if not camp.composing_module:
        rep.check("STRUCTURE", None,
                  "NO composing module -- af-ml-campaign-loop.sh CANNOT drive this campaign. "
                  "It is one-arm-at-a-time and needs the af-ml-supervise AGENT.",
                  composing_module="none", loop_drivable="false", detail=camp.dispatch)
        return
    code, out, err = _lab_probe(
        repo, f"import importlib.util as u; print('SPEC ' + str(u.find_spec({camp.composing_module!r}) is not None))")
    found = _last_line(out) == "SPEC True"
    rep.check("STRUCTURE", found,
              (f"composing module {camp.composing_module} imports; "
               f"af-ml-campaign-loop.sh can drive it with AF_DISPATCH=\"{camp.dispatch}\""
               if found else f"composing module {camp.composing_module} does NOT import"),
              composing_module=camp.composing_module, loop_drivable=str(found).lower(),
              exit=code, detail=camp.dispatch if found else (_last_line(err) or "not importable"))


# --------------------------------------------------------------------------- driver

def preflight(
    name: str,
    repo: Path,
    praxis: Path,
    *,
    campaigns: dict[str, Campaign] | None = None,
    refused: dict[str, str] | None = None,
) -> tuple[int, Report]:
    campaigns = CAMPAIGNS if campaigns is None else campaigns
    refused = FORBIDDEN if refused is None else refused
    rep = Report(name)
    if name in refused:
        rep.check("REFUSAL", False,
                  f"campaign {name!r} is refused by the project manifest",
                  campaign=name, detail=refused[name])
        return REFUSED, rep
    camp = campaigns.get(name)
    if camp is None:
        rep.check("REFUSAL", False,
                  f"unknown campaign {name!r}; known: {', '.join(sorted(campaigns))}",
                  campaign=name)
        return REFUSED, rep

    space = repo / camp.space
    ledger = repo / camp.ledger
    if not space.is_file():
        rep.check("STATUS", False, f"no space file at {space}", path=space)
        return NOT_READY, rep

    model_meta = json.loads(space.read_text())
    model = next((f for f in model_meta["facts"]
                  if f.get("category") == "model" and f.get("id") == camp.model_id), None)
    if model is None:
        rep.check("STATUS", False, f"model {camp.model_id} not in {space}", path=space)
        return NOT_READY, rep
    stages = list(model["meta"].get("stages") or [])
    baseline_runs = list(model["meta"].get("baseline_runs") or [])

    st = _check_status(rep, praxis, space, camp.model_id)
    trials = st["trials_total"] if st else 0
    untried = len(st["ideas_by_status"].get("untried", [])) if st else 0
    if stages:
        _check_complete(rep, praxis, space, camp.model_id, stages, trials)
    else:
        rep.check("COMPLETE", False, "the model declares no stages, so campaign-complete has "
                  "nothing to close", stages="none")
    _check_ledger(rep, ledger, baseline_runs)
    _check_seed_drift(rep, space)
    _check_corpus(rep, repo, camp)
    _check_dispatch(rep, repo, camp, ledger, untried)
    _check_structure(rep, repo, camp)

    if rep.n_fail:
        return NOT_READY, rep
    return (READY, rep) if rep.runnable_arms > 0 else (NO_RUNNABLE_ARMS, rep)


_VERDICT = {READY: "READY", NOT_READY: "NOT_READY", REFUSED: "REFUSED",
            NO_RUNNABLE_ARMS: "NOT_READY", USAGE: "NOT_READY"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="af-ml-campaign-preflight",
        description="Can each campaign actually run right now, on THIS machine, with evidence.")
    ap.add_argument("--manifest", type=Path,
                    help="versioned project preflight manifest (schema_version 1)")
    ap.add_argument("--campaign", action="append", default=[],
                    help="campaign name from the manifest; repeatable")
    ap.add_argument("--all", action="store_true", help="every campaign in the manifest")
    ap.add_argument("--project-root", type=Path,
                    help="checkout holding project state and probe implementations")
    ap.add_argument("--repo", default=os.environ.get("AF_SPORTS_REPO", ""),
                    help="sports_analysis checkout holding the spaces, ledgers and labs")
    ap.add_argument("--praxis", default=os.environ.get("PRAXIS", ""),
                    help="praxis checkout providing knowledge.ml_registry.cli")
    args = ap.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest) if args.manifest else PreflightManifest(
            1, CAMPAIGNS, FORBIDDEN
        )
    except ValueError as exc:
        ap.error(str(exc))
    names = list(manifest.campaigns) if args.all else list(args.campaign)
    if not names:
        print("PREFLIGHT ALL RESULT NOT_READY exit=2 detail=pass --campaign NAME or --all")
        print("give --campaign NAME (repeatable) or --all", file=sys.stderr)
        return USAGE

    praxis = Path(args.praxis) if args.praxis else Path(__file__).resolve().parents[2]
    repo = args.project_root or (Path(args.repo) if args.repo else _guess_repo())
    if repo is None or not repo.is_dir():
        print("PREFLIGHT ALL RESULT NOT_READY exit=2 detail=sports_analysis_repo_not_found")
        print("could not locate the sports_analysis checkout; pass --repo", file=sys.stderr)
        return USAGE

    print(f"preflight on {os.uname().nodename}  repo={repo}  praxis={praxis}", file=sys.stderr)
    worst, ready = READY, 0
    for name in names:
        code, rep = preflight(
            name, repo, praxis, campaigns=manifest.campaigns, refused=manifest.refused
        )
        for line in rep.lines:
            print(line, flush=True)
        print(f"PREFLIGHT {name} RESULT {_VERDICT[code]} exit={code} pass={rep.n_pass} "
              f"fail={rep.n_fail} runnable_arms={rep.runnable_arms}", flush=True)
        print(f"\n{name}: {_VERDICT[code]} (exit {code}, {rep.n_pass} pass / {rep.n_fail} fail, "
              f"{rep.runnable_arms} runnable arm(s))", file=sys.stderr)
        for line in rep.human:
            print(line, file=sys.stderr)
        if code == READY:
            ready += 1
        if _SEVERITY[code] > _SEVERITY[worst]:
            worst = code
    print(f"PREFLIGHT ALL RESULT {_VERDICT[worst]} exit={worst} campaigns={len(names)} "
          f"ready={ready}", flush=True)
    print(f"\nOVERALL {_VERDICT[worst]} (exit {worst}): {ready} of {len(names)} campaign(s) "
          f"ready to dispatch unattended on this machine.", file=sys.stderr)
    return worst


def _guess_repo() -> Path | None:
    for candidate in (Path("/workspace/sports_analysis"),
                      Path.home() / "Documents/official_repos/sports_analysis"):
        if (candidate / "ml").is_dir():
            return candidate
    return None


if __name__ == "__main__":
    raise SystemExit(main())
