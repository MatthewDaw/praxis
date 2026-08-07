"""The ingestion API — the sole writer of validation/knowledge content (FL1 / KD1 / KD3).

FL1 lays the foundation KD1 and R1's later ingestion sequence build on: a shared org-level
``factory-learnings`` space holds lessons (and, eventually, the failure-class taxonomy), it is
cloud-canonical (Praxis, never a local file), and it is mounted READ-ONLY into every project
space at claim/resolve time (see ``hooks._ticket_state.start_ticket``). This module is the ONLY
place in the codebase that writes into that space — :func:`write_lesson` is the sole write path.
Reads (:func:`read_lessons`) are plain GETs, which nothing can turn into a write, so any project
session that wants to see a lesson goes through them (or the read-only mount) and never through a
write endpoint.

FL4 (R7) extends this same sole-writer module with the bad-artifact pin: at regression time the
failing commit is bundled into a SELF-CONTAINED git reproduction bundle plus secret-scanned
diff/evidence text, written into the ``artifacts`` snapshot of the same shared space
(:func:`pin_artifact`), so proof and future re-proof (KD7) always have something to run against.

The full ingestion SEQUENCE (classify/dedup, draft a check, attempt fail-then-pass proof, bind,
activate, regress matching tickets — R1/R2) lands with FL2 onward; this module carries only the
lesson/artifact read/write primitives and the CLI shell those later tickets extend.
"""

from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from hooks import _praxis

LESSON_CATEGORY = "lesson"
ARTIFACT_CATEGORY = "artifact"
DEFAULT_RETENTION_DAYS = 90

# Targeted secret-scan patterns (R7): each replaces ONLY the secret-shaped substring it matches,
# never the whole text — a blanket blank would erase the failure signal the evidence exists to
# preserve. This is the named exception to FL2's blanket-redaction rule; it applies to the
# diff/evidence TEXT only, never to the reproduction bundle itself (redacting the actual failing
# tree would break reproduction).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),                                      # AWS access key id
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),                            # GitHub tokens
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                                   # OpenAI/Anthropic-shaped keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.=]{10,}"),                    # bearer tokens
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*"
               r"['\"]?[A-Za-z0-9/_.\-]{8,}['\"]?"),                      # generic key: value / key=value
)


def write_lesson(text: str, *, source: str | None = None,
                 meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write ``text`` as a lesson into the shared ``factory-learnings`` space (POST /insights).

    THE sole write path into that space (R1/KD3): nothing else in this codebase is allowed to
    target ``(hooks._praxis.FACTORY_LEARNINGS_SPACE, hooks._praxis.FACTORY_LEARNINGS_SNAPSHOT)``
    with a write. Idempotently bootstraps the space on first use (a space that has never been
    created 404s on its first snapshot-bound write). Returns the server's insight-write ack
    (``{"summary","action","id",...}``).
    """
    body = str(text or "").strip()
    if not body:
        raise ValueError("text is required")
    _praxis.ensure_space(_praxis.FACTORY_LEARNINGS_SPACE, name="factory-learnings")
    payload: dict[str, Any] = {
        "insight": body,
        "category": LESSON_CATEGORY,
        "source": source,
        "meta": meta or {},
    }
    return _praxis._request(
        "POST", "/insights", body=payload,
        space=_praxis.FACTORY_LEARNINGS_SPACE, snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT,
    )


def read_lessons(query: str = "", *, top_k: int = 10) -> list[dict[str, Any]]:
    """Read-only lessons lookup against the shared ``factory-learnings`` space.

    A GET-only call (``context``/``facts_by``): there is no write side-effect, so this is safe to
    call from any project session without granting it a write path. With a non-empty ``query`` this
    is similarity-ranked (top ``top_k``); with an empty query it is the exhaustive active-lesson
    enumeration.
    """
    q = (query or "").strip()
    if q:
        return _praxis.context(q, top_k=top_k, space=_praxis.FACTORY_LEARNINGS_SPACE,
                               snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    return _praxis.facts_by(category=LESSON_CATEGORY, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


def redact_secrets(text: str) -> str:
    """Targeted secret-scan redaction (R7): replace only the secret-shaped substrings in ``text``,
    never the whole text — evidence minus its secrets must still explain the failure."""
    out = text or ""
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def build_repro_bundle(repo_path: str | Path, commit_sha: str) -> bytes:
    """A SELF-CONTAINED git bundle reproducing ``commit_sha`` and its full ancestry (R7): bundled
    under a throwaway ref so it re-materializes the failing tree via ``git clone`` on a machine
    that never held ``repo_path``'s loose objects (worktree branches are reaped and their commits
    GC-eligible once the ticket ships). The throwaway ref is created and deleted around the bundle
    call so ``repo_path`` — which may be the live project checkout — is left exactly as found.
    """
    repo_path = str(repo_path)
    ref = f"refs/heads/repro-pin-{uuid.uuid4().hex}"
    subprocess.run(["git", "-C", repo_path, "update-ref", ref, commit_sha],
                   check=True, capture_output=True, text=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "repro.bundle"
            subprocess.run(["git", "-C", repo_path, "bundle", "create", str(bundle_path), ref],
                           check=True, capture_output=True, text=True)
            return bundle_path.read_bytes()
    finally:
        subprocess.run(["git", "-C", repo_path, "update-ref", "-d", ref],
                       check=True, capture_output=True, text=True)


def materialize_bundle(bundle_bytes: bytes, dest_dir: str | Path) -> Path:
    """Re-materialize a pinned bundle's failing tree by cloning it into ``dest_dir`` — the step
    proof/re-proof execution runs against (R7), pointed at a disposable directory that never
    touched the origin repo's objects. Returns the checked-out clone's path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = dest_dir / "_repro.bundle"
    bundle_path.write_bytes(bundle_bytes)
    clone_dir = dest_dir / "repro"
    ref = _bundle_ref_name(bundle_path)
    subprocess.run(["git", "clone", "--quiet", "--branch", ref, str(bundle_path), str(clone_dir)],
                   check=True, capture_output=True, text=True)
    return clone_dir


def _bundle_ref_name(bundle_path: Path) -> str:
    """The short branch name a :func:`build_repro_bundle` bundle carries, read back via ``git
    bundle list-heads`` so :func:`materialize_bundle` never has to guess/hardcode the throwaway
    ref (``git clone --branch`` wants the short name, not the full ``refs/heads/...`` path)."""
    out = subprocess.run(["git", "bundle", "list-heads", str(bundle_path)],
                         check=True, capture_output=True, text=True).stdout
    first_line = out.strip().splitlines()[0]  # "<sha> refs/heads/repro-pin-<uuid>"
    full_ref = first_line.split(" ", 1)[1].strip()
    return full_ref.removeprefix("refs/heads/")


def pin_artifact(*, project: str, ticket_id: str, commit_sha: str, repo_path: str | Path,
                 diff_text: str = "", evidence_text: str = "", while_gating: bool = True,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 source: str | None = None) -> dict[str, Any]:
    """Pin the bad artifact at regression time (R7): a self-contained reproduction bundle over
    ``commit_sha`` plus secret-scanned diff/evidence text, written into cloud storage (the shared
    space's ``artifacts`` snapshot) retained per the default policy — kept while its check is
    gating, else expiring ``retention_days`` (default 90) after pinning, with expiry observable
    via the stored ``retention_expires_at`` (see :func:`artifact_expired`). The bundle itself is
    left UNTOUCHED by redaction (breaking reproduction is worse than a leaked secret in evidence
    prose); only ``diff_text``/``evidence_text`` are scanned. Returns the write ack including the
    new artifact's id."""
    if not commit_sha:
        raise ValueError("commit_sha is required")
    bundle_bytes = build_repro_bundle(repo_path, commit_sha)
    pinned_at = time.time()
    meta: dict[str, Any] = {
        "project": project,
        "ticket_id": ticket_id,
        "commit_sha": commit_sha,
        "bundle_b64": base64.b64encode(bundle_bytes).decode("ascii"),
        "diff": redact_secrets(diff_text),
        "evidence": redact_secrets(evidence_text),
        "pinned_at": pinned_at,
        "while_gating": bool(while_gating),
        "retention_days": int(retention_days),
        "retention_expires_at": pinned_at + int(retention_days) * 86400,
    }
    _praxis.ensure_space(_praxis.FACTORY_LEARNINGS_SPACE, name="factory-learnings")
    return _praxis._request(
        "POST", "/insights",
        body={"insight": f"pinned artifact for {project}/{ticket_id} @ {commit_sha}",
              "category": ARTIFACT_CATEGORY, "source": source, "meta": meta},
        space=_praxis.FACTORY_LEARNINGS_SPACE, snapshot=_praxis.FACTORY_ARTIFACTS_SNAPSHOT,
    )


def read_artifact(artifact_id: str) -> dict[str, Any]:
    """Read one pinned artifact back (GET) — the path proof/re-proof execution reads the bundle
    and evidence from (R7); read-only, same sole-writer guarantee as :func:`read_lessons`."""
    return _praxis.get_fact(artifact_id, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_ARTIFACTS_SNAPSHOT)


def artifact_expired(meta: dict[str, Any], *, now: float | None = None) -> bool:
    """The default retention policy (R7/KD7), as a pure function of a pinned artifact's stored
    meta: never expired while its check is gating; otherwise expired once ``now`` passes the
    stored ``retention_expires_at`` — observable without any lifecycle sweep running first."""
    if bool(meta.get("while_gating")):
        return False
    expires_at = meta.get("retention_expires_at")
    if expires_at is None:
        return False
    return (now if now is not None else time.time()) > float(expires_at)


def decode_bundle(meta: dict[str, Any]) -> bytes:
    """Decode a pinned artifact's stored bundle back to raw bytes for :func:`materialize_bundle`."""
    return base64.b64decode(str(meta.get("bundle_b64") or ""))


def _cmd_ingest(args: argparse.Namespace) -> int:
    result = write_lesson(args.text, source=args.source)
    print(result.get("summary") or result.get("id") or "ok")
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    for hit in read_lessons(args.query, top_k=args.top_k):
        text = str(hit.get("text") or "")
        print(f"{hit.get('id', '')}\t{text}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agent_factory.ingestion_api",
        description="The sole writer of the shared factory-learnings space (FL1). "
                    "The full classify/dedup/draft/proof/bind/activate sequence (R1) lands with FL2+.")
    sub = ap.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="write a lesson into the factory-learnings space")
    ingest.add_argument("text", help="the lesson text")
    ingest.add_argument("--source", default=None, help="provenance pointer for the lesson")
    ingest.set_defaults(func=_cmd_ingest)

    read = sub.add_parser("read", help="read lessons from the factory-learnings space (read-only)")
    read.add_argument("query", nargs="?", default="", help="similarity query; omit for all lessons")
    read.add_argument("--top-k", type=int, default=10, dest="top_k")
    read.set_defaults(func=_cmd_read)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
