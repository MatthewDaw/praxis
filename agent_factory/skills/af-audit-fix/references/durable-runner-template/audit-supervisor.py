#!/usr/bin/env python3
"""Run bounded MVPVU audit turns from the canonical Praxis checkpoint."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT.parent
BUILD = "993b78598d2c3b82bf569b441f2b4836a2e63a07"
PIN = BUILD[:12]
RUN = f"post-adoption-{PIN}"
WORKTREE = BUNDLE / f"worktree-{PIN}"
BROWSER = BUNDLE / f"browser-{RUN}"
FIXTURE = BUNDLE / "fixture-audit2511b.private.json"
ORG_ID = "07672b91-a9e3-424b-9f55-d7e957fa3be6"
DATABASE = f"mvpvu_audit_{PIN}"
ORIGIN = "http://127.0.0.1:5190"
FACTORY = Path.home() / "Documents/official_repos/praxis/agent_factory"
SKILL = Path.home() / ".agents/skills/af-audit-fix/SKILL.md"
CODEX = Path.home() / ".nvm/versions/node/v22.21.1/bin/codex"
NODE = CODEX.parent / "node"
SECURE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
PAUSE = ROOT / "paused"
LEASE = ROOT / "coordinator.lock"
CHECKPOINT = ROOT / "checkpoint.json"
HANDOFF = ROOT / "fix-handoff"
TOOL_PATH = f"{CODEX.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
READY_RETRIES = {
    "LOCAL-ATTENDANCE-EVENTS": "linked-attendance-fixture",
    "LOCAL-PART-DIVISION": "accepted-unplaced-fixture",
    "LOCAL-PART-GUEST": "accepted-guest-fixture",
}
SWEEP_VALIDATION_ERRORS = {
    "sweep target or build differs", "explicit role, sport, viewport and fixtureState required",
    "invalid sweep timing", "use a local route without query parameters", "unknown sweep case",
    "incomplete action evidence", "invalid profile scope",
    "shared checks need verified behavior and an explicit equivalence basis",
    "remaining steps must reference exercised scenarios", "redact sweep evidence",
}


class InvalidEvidence(Exception):
    """A permanently invalid submission. Preserve its artifacts without blocking the queue."""


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def ledger(command, *arguments):
    cli = [
        str(FACTORY / ".venv/bin/python"), "-m", "agent_factory.af_audit_fix",
        "--project", "mvpvu-localhost-audit-20261121", "--org", "sports-analysis",
        "--run", RUN, command, *arguments,
    ]
    result = subprocess.run([
        "/bin/bash", "-c",
        'set -e; set -a; source "$HOME/.praxis/sports-analysis.env"; set +a; exec "$@"',
        "praxis", *cli,
    ], capture_output=True, timeout=45)
    if result.returncode:
        if command == "record-sweep" and result.returncode == 2:
            try:
                reason = json.loads(result.stdout).get("error")
            except (ValueError, AttributeError):
                reason = None
            if isinstance(reason, str) and reason in SWEEP_VALIDATION_ERRORS:
                raise InvalidEvidence(reason)
        raise RuntimeError(f"Praxis {command} unavailable (exit {result.returncode})")
    return json.loads(result.stdout)


def read_health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{ORIGIN}/v1/health", timeout=5) as response:
        return json.load(response)


def local_provider_proof():
    proof = ROOT / "turns" / f"provider-proof-active-{PIN}" / "provider-browser-proof.json"
    if not proof.exists():
        return {}
    receipt = json.loads(proof.read_text())
    if (receipt.get("origin"), receipt.get("build")) != (ORIGIN, BUILD):
        return {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{ORIGIN}/__audit-provider/stripe/ready", timeout=3) as response:
            return receipt if response.status == 204 else {}
    except OSError:
        return {}


def recover_missing_stack():
    stack = BUNDLE / f"stack-{RUN}"
    for component, port in (("api", 18091), ("web", 5190)):
        occupied = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", f"-iTCP:{port}",
                                   "-sTCP:LISTEN", "-t"], capture_output=True, timeout=10)
        if occupied.returncode == 0:
            continue
        with (stack / f"{component}-recovery.log").open("ab") as log:
            child = subprocess.Popen(["/bin/bash", ROOT / "recover-stack.sh",
                                      WORKTREE, BUILD, component], stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (stack / f"{component}-recovery.pid").write_text(f"{child.pid}\n")
    raise RuntimeError("Audit proxy unavailable; restarted missing owned components, if any")


def check_scope():
    head = subprocess.check_output(["/usr/bin/git", "-C", WORKTREE, "rev-parse", "HEAD"], text=True).strip()
    if head != BUILD:
        raise RuntimeError("Audit worktree build differs from the configured SHA")
    fixture = json.loads(FIXTURE.read_text())
    readiness = json.loads((BUNDLE / "fixture-audit2511b-readiness-final.json").read_text())
    if (readiness.get("build"), readiness.get("origin"), readiness.get("organization", {}).get("id"),
            readiness.get("fixtureKey")) != (BUILD, ORIGIN, ORG_ID, fixture.get("fixtureKey")):
        raise RuntimeError("Browser readiness does not match the pinned fixture")
    if not readiness.get("signIns", {}).get("orgadmin", {}).get("matchesOwnedActor"):
        raise RuntimeError("Browser fixture readiness has not verified the pinned build and roles")
    if (fixture.get("database"), fixture.get("origin"), fixture.get("organization", {}).get("id")) != (DATABASE, ORIGIN, ORG_ID):
        raise RuntimeError("Audit fixture manifest does not match the owned organization")
    try:
        health = read_health()
    except OSError:
        recover_missing_stack()
    if health.get("status") != "ok" or health.get("buildId") != BUILD:
        raise RuntimeError("Audit proxy does not serve the pinned build")
    return fixture_revisions(fixture, readiness, local_provider_proof())


def fixture_revisions(fixture, readiness, provider):
    """Fingerprint only scoped fixture proofs, without identity credentials."""
    owned = fixture["owned"]
    roster = owned.get("roster", {})
    events = owned.get("events", {})
    sign_ins = readiness["signIns"]
    proofs = {
        "linked-attendance-fixture": (("orgadmin", "player"),
            [roster.get("player", {}).get("roster"), roster.get("player", {}).get("athlete"),
             events.get("Shared game"), events.get("Practice"), events.get("Meeting")]),
        "accepted-unplaced-fixture": (("unplaced",),
            [roster.get("unplaced", {}).get("enrollment"), events.get("Shared game"),
             events.get("Shared counterpart")]),
        "accepted-guest-fixture": (("guest",),
            [owned.get("guestInvite"), events.get("Shared game")]),
    }
    revisions = {}
    for name, (roles, records) in proofs.items():
        role_proofs = [sign_ins.get(role, {}) for role in roles]
        if all(records) and all(proof.get("matchesOwnedActor") and proof.get("session") == 200 for proof in role_proofs):
            payload = [BUILD, ORG_ID, fixture["fixtureKey"], records, role_proofs]
            revisions[name] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    identity = [BUILD, ORG_ID, fixture["fixtureKey"]]
    additional = {
        "browser-identity": sign_ins,
        "controlled-inbox": readiness.get("inbox", {}),
        "payment-sandbox": provider.get("payments", {}),
        "authenticator-sign-in": provider.get("mfa", {}),
    }
    for name, proof in additional.items():
        if proof:
            revisions[name] = hashlib.sha256(json.dumps([identity, proof], sort_keys=True).encode()).hexdigest()[:16]
    return revisions


def seatbelt(extra=()):
    # macOS applies the outer profile to Codex and all tool/browser descendants.
    # Deny all network except the disposable app, DNS, and TLS for Praxis/model.
    codex_home = Path.home() / ".codex"
    paths = [ROOT / "browser-tmp", ROOT / "turns", BROWSER / "supervised-evidence",
             *(str(path) for path in extra), codex_home, Path("/private/var/folders")]
    private_config = ("auth.json", "config.toml", "rules", "hooks.json", "skills", "plugins",
                      "sessions", "archived_sessions", "worktrees", "session_index.jsonl", "history.jsonl")
    rules = [
        "(version 1)", "(allow default)",
        "(deny network-outbound)",
        '(allow network-outbound (remote ip "localhost:5190") (remote tcp "*:443")'
        ' (remote unix-socket (path-literal "/private/var/run/mDNSResponder"))'
        ' (remote unix-socket (path-literal "/var/run/mDNSResponder")))',
        "(deny file-write*)",
        f"(allow file-write* (literal {json.dumps(str(codex_home))}))",
        '(allow file-write* (literal "/dev/null") (literal "/dev/tty") '
        + " ".join(f"(subpath {json.dumps(str(path))})" for path in paths) + ")",
        "(deny file-write* " + " ".join(
            f"(subpath {json.dumps(str(codex_home / name))})" for name in private_config
        ) + ")",
    ]
    return "\n".join(rules) + "\n"


def model_turn(directory, prompt, *, cwd=WORKTREE, extra_write=(), seconds=600, lease=None):
    profile = directory / "worker.sb"
    profile.write_text(seatbelt(extra_write))
    arguments = [
        "/usr/bin/sandbox-exec", "-f", str(profile), str(CODEX), "exec",
        "--ignore-user-config", "--ephemeral", "--json",
        "-s", "danger-full-access", "-c", "approval_policy=never",
        "-C", str(cwd), "-",
    ]
    with (directory / "events.jsonl").open("w") as events, (directory / "stderr.log").open("w") as errors:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                                   env={**os.environ, "PATH": TOOL_PATH},
                                   start_new_session=True, pass_fds=(lease.fileno(),) if lease else ())
        try:
            process.communicate(prompt.encode(), timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise RuntimeError("Bounded Codex turn expired")
    if process.returncode:
        raise RuntimeError(f"Bounded Codex turn exited {process.returncode}")
    found_turn = False
    with (directory / "events.jsonl").open() as events:
        for line in events:
            try:
                found_turn |= json.loads(line).get("type") == "turn.completed"
            except ValueError:
                continue
    if not found_turn:
        raise RuntimeError("No completed model turn was recorded")


def choose(queue, checkpoint, status, revisions):
    attempted = checkpoint.get("attempted", {})
    attempted_revision = checkpoint.get("attempted_fixture_revision", {})
    def cooled(case_id):
        return time.time() - attempted.get(case_id, 0) > 600

    originals = set(status["run_meta"]["inventory_ids"])
    ready = []
    for case in status["cases"]:
        case_id = case["case_id"]
        initial = case["initial"]
        if initial["status"] != "blocked" or not SECURE_ID.fullmatch(case_id):
            continue
        dependencies = initial.get("blocking_prerequisites") or [READY_RETRIES.get(case_id)]
        if (not isinstance(dependencies, list) or not dependencies
                or not all(isinstance(dependency, str) and dependency in revisions for dependency in dependencies)):
            continue
        generation = ":".join(revisions[dependency] for dependency in sorted(set(dependencies)))
        if generation != attempted_revision.get(case_id) and generation != initial.get("prerequisite_revision"):
            ready.append((case_id, generation))
    # Prefer a recovered original. Take one targeted fixture retry per attempt so a
    # blocked or cooling case cannot hold up another case or fill the batch.
    ready.sort(key=lambda item: item[0] not in originals)
    if ready:
        case_id, generation = ready[0]
        return [case_id], {"state": "fixture-ready", "prerequisite_revision": generation}, generation

    for batch in queue.get("batches", []):
        ids = [case_id for case_id in batch["case_ids"] if SECURE_ID.fullmatch(case_id) and cooled(case_id)]
        if ids and batch.get("state") in {"pending", "retry"}:
            return ids[:4], batch, None
    return None


def submit_turn_records(turn_dir, ids):
    for request in sorted(Path(turn_dir).glob("submit-*.json")):
        receipt = request.with_name("receipt-" + request.name)
        rejected = request.with_name("rejected-" + request.name)
        if receipt.exists() or rejected.exists():
            continue
        try:
            try:
                envelope = json.loads(request.read_text())
                command = envelope["command"]
                if command not in {"record-batch", "record-sweep"}:
                    raise InvalidEvidence("Invalid record command")
                payload = Path(envelope["path"]).resolve()
                if not payload.is_relative_to(request.parent.resolve()):
                    raise InvalidEvidence("Audit evidence is outside its assigned turn")
                body = json.loads(payload.read_text())
                if command == "record-batch":
                    submitted = [row["case_id"] for row in body]
                    builds = [row["result"]["build"] for row in body]
                else:
                    submitted = list(body.get("remaining", {}))
                    submitted.extend(action.get("caseId") for visit in body.get("visits", [])
                                     for action in visit.get("actions", []))
                    builds = [body.get("build")]
                if not submitted or not set(submitted) <= set(ids) or set(builds) != {BUILD}:
                    raise InvalidEvidence("Submitted evidence does not match assigned cases and build")
            except (ValueError, KeyError, AttributeError, TypeError, FileNotFoundError):
                raise InvalidEvidence("Audit submission has a missing or invalid payload") from None
            write_json(receipt, ledger(command, "--input", str(payload)))
        except InvalidEvidence as error:
            # Keep the original pending file and evidence. This is a local
            # diagnostic, never a canonical Praxis success receipt.
            write_json(rejected, {"state": "invalid_evidence", "reason": str(error)})


def enqueue_confirmed_failures(ids, turn_dir, checks):
    for case in checks if isinstance(checks, list) else [checks]:
        case_id = case.get("case_id", "")
        if case_id not in ids or case.get("result", {}).get("status") != "failed":
            continue
        path = HANDOFF / f"{case_id}.json"
        if not path.exists():
            write_json(path, {"build": BUILD, "run": RUN, "case_id": case_id,
                              "case": case, "turn": str(turn_dir)})


def audit_prompt(ids, batch, details, directory, scenarios):
    return f"""Run one bounded functional MVPVU audit batch using {SKILL}. Read the audit skill.
Pinned {BUILD}, run {RUN}, origin {ORIGIN}, synthetic organization {ORG_ID}.
Case IDs {json.dumps(ids)}. Batch context {json.dumps(batch)}. Scenario contracts {json.dumps(scenarios)}. Canonical case details {json.dumps(details)}.
Check current owner behavior against historical expectations. MVPVU now uses Organization > Registration form for the official form and Division > Forms for placements. A retired Registry Forms URL never by itself makes an intended replacement behavior a failure; record changed/superseded route evidence and keep incomplete functional checks partial.
Use only the pinned app and existing synthetic org. Use the actual allowed role from private fixture manifest {FIXTURE}.
Permission is granted for all routine application operations inside that disposable organization, including creating, modifying, resetting and archiving run-owned synthetic fixtures through the app. When an eligible fixture is missing, create it through the supported UI rather than asking for permission. Verify the affected role can use it, and clean up only your own temporary records.
You may access that org only. Never use another organization or mutate shared services. Never access review, production, external providers, or direct databases. Never run security probes or exploit reproduction. Use normal functional app paths.
When the payment simulation health check {ORIGIN}/__audit-provider/stripe/ready is available, use the normal payment workflows and same-origin simulated Stripe pages. They offer synthetic approval, decline and cancellation. Do not enter real card data or have contact with a provider. The sign-in MFA mock, when ready, has one dedicated auditor account in {BUNDLE}/stack-post-adoption-993b78598d2c/mfa-actor.private.json; compute its synthetic code in memory via providers/totp.mjs. Never print credentials or codes. Case assertions still apply. MFA enrollment, passkey and external-delivery cases require their own coverage.
Record every completed case's real evidence and observed console/network outcomes. For a partial check collect private sweep evidence via record-sweep (no full verdict) and precisely what remains; for required missing external providers record a blocker promptly and continue the other cases. If retrying on a fixture revision, record that prerequisite_revision for any remaining blocker and give its exact blocking_prerequisites. Never use a blocked case or infrastructure failure as evidence of product failure. Nothing passed without evidence.
Use Playwright installation {BROWSER}/node_modules and write a temporary .mjs script under {directory}; browser can only access the pinned product. For sweep evidence use runPageSweep from {SKILL.parent}/scripts/page-sweep.mjs. It captures actual start and browser timings and keeps routes free of query parameters. Use assigned case IDs in each action and use the start time from AF_AUDIT_BATCH_STARTED_MS. Do not print credentials, raw headers or private authentication files. Keep sanitized evidence in {BROWSER}/supervised-evidence or {directory}.
Submit canonical verdicts only for assigned IDs and only after the pinned work is verified. Run {ROOT}/record-results.sh with a JSON payload file and optional second argument record-sweep; it validates build and case scope and queues a canonical submission for the supervisor. Keep original verdicts historical. Do not change Git, migrations, other worktrees, or the app build. A separate repair stage takes each newly reproduced failure.
Finish within one turn. Report assigned IDs, checked evidence, blocked prerequisites, and cleanup.
"""


def main():
    if PAUSE.exists():
        return
    with LEASE.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        checkpoint = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {}
        if time.time() < checkpoint.get("next_at", 0):
            return
        turn = None
        try:
            revisions = check_scope()
            if (checkpoint.get("pending_submission") or checkpoint.get("state") == "running") and checkpoint.get("case_ids"):
                previous_ids = [case_id for case_id in checkpoint["case_ids"] if SECURE_ID.fullmatch(case_id)]
                if previous_ids:
                    submit_turn_records(checkpoint["last_turn"], previous_ids)
                    enqueue_confirmed_failures(previous_ids, checkpoint["last_turn"],
                                               ledger("check", "--case", *previous_ids))
                checkpoint.pop("pending_submission", None)
            status = ledger("status")
            queue = ledger("queue")
            if (queue.get("build") != BUILD or status.get("phase") != "audit"
                    or status.get("run_meta", {}).get("initial_build") != BUILD):
                raise RuntimeError("Praxis run is not auditing the configured SHA")
            turn = choose(queue, checkpoint, status, revisions)
            if turn is None:
                checkpoint.update(state="waiting", next_at=time.time() + 60)
                return
            ids, batch, generation = turn
            turn_dir = ROOT / "turns" / f"{time.time_ns()}"
            turn_dir.mkdir(mode=0o700)
            write_json(turn_dir / "assignment.json", {"build": BUILD, "run": RUN, "case_ids": ids})
            details = ledger("check", "--case", *ids)
            selected = [case for case in status["cases"] if case["case_id"] in ids]
            if generation:
                # Praxis updates an initial blocked verdict in place. Save its
                # evidence before handing the case to the fresh fixture.
                write_json(turn_dir / "prior-blockers.json",
                           {case["case_id"]: case["initial"] for case in selected})
            checkpoint.update(state="running", last_started=time.time(), last_turn=str(turn_dir),
                              case_ids=ids, pending_submission=True, next_at=0)
            checkpoint.setdefault("attempted", {}).update(dict.fromkeys(ids, time.time()))
            if generation:
                checkpoint.setdefault("attempted_fixture_revision", {}).update(dict.fromkeys(ids, generation))
            write_json(CHECKPOINT, checkpoint)
            env = os.environ
            env["MVPVU_AUDIT_ASSIGNMENT"] = str(turn_dir / "assignment.json")
            env["AF_AUDIT_BATCH_STARTED_MS"] = str(int(checkpoint["last_started"] * 1000))
            try:
                model_turn(turn_dir, audit_prompt(ids, batch, details, turn_dir,
                                                [case["scenario"] for case in selected]), lease=lock)
            finally:
                env.pop("MVPVU_AUDIT_ASSIGNMENT", None)
                env.pop("AF_AUDIT_BATCH_STARTED_MS", None)
            submit_turn_records(turn_dir, ids)
            refreshed = ledger("check", "--case", *ids)
            enqueue_confirmed_failures(ids, turn_dir, refreshed)
            checkpoint.pop("pending_submission", None)
            checkpoint.update(state="resumable", last_completed=time.time(), retry=0,
                              next_at=time.time() + 15)
            checkpoint.pop("error", None)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            attempts = checkpoint.get("retry", 0) + 1
            # Persistent capped delay after a restart or external outage.
            checkpoint.update(state="retry", retry=attempts, error=type(error).__name__,
                              next_at=time.time() + min(900, 15 * 2 ** min(attempts, 6)))
            if isinstance(error, RuntimeError):
                checkpoint["error"] = str(error)
        finally:
            write_json(CHECKPOINT, checkpoint)


if __name__ == "__main__":
    os.umask(0o077)
    os.environ["TMPDIR"] = str(ROOT / "browser-tmp")
    os.environ["PATH"] = f"{CODEX.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    if sys.argv[1:] == ["--smoke"]:
        check_scope()
        assert ledger("queue")["build"] == BUILD
        directory = ROOT / "turns" / f"smoke-{time.time_ns()}"
        directory.mkdir(mode=0o700)
        model_turn(directory, f"Use one command only: /usr/bin/curl --noproxy '*' --silent --fail {ORIGIN}/v1/health . Report the status field value only.", seconds=90)
        print(directory)
    else:
        main()
