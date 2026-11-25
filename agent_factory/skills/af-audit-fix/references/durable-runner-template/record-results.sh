#!/bin/bash
set -euo pipefail
umask 077
directory="$(cd "$(dirname "$0")" && pwd)"
factory="$HOME/Documents/official_repos/praxis/agent_factory"
assignment="${MVPVU_AUDIT_ASSIGNMENT:?Audit runner assignment not found}"
verdicts="${1:?Use a local JSON audit evidence payload}"
command="${2:-record-batch}"
[[ "$command" == record-batch || "$command" == record-sweep ]]
"$factory/.venv/bin/python" - "$assignment" "$verdicts" "$directory" "$command" <<'PY'
import json
from pathlib import Path
import sys
import time
assignment, verdicts, root = map(Path, sys.argv[1:4])
command = sys.argv[4]
turns = (root / "turns").resolve()
if not assignment.resolve().is_relative_to(turns):
    raise SystemExit("Invalid assignment path")
if not verdicts.resolve().is_relative_to(assignment.parent.resolve()):
    raise SystemExit("Verdicts must be stored with this turn’s evidence")
a = json.loads(assignment.read_text())
rows = json.loads(verdicts.read_text())
if command == "record-batch":
    ids = [row["case_id"] for row in rows]
    if not ids or len(ids) != len(set(ids)) or not set(ids) <= set(a["case_ids"]):
        raise SystemExit("Verdict does not belong to this turn")
    if any(row["result"]["build"] != a["build"] for row in rows):
        raise SystemExit("Verdict build does not match this turn")
else:
    ids = set(rows.get("remaining", {}))
    for visit in rows.get("visits", []):
        for action in visit.get("actions", []):
            ids.add(action.get("caseId"))
    if None in ids:
        raise SystemExit("Every sweep action must name an assigned case")
    if not ids or not ids <= set(a["case_ids"]) or rows.get("build") != a["build"]:
        raise SystemExit("Sweep does not belong to this turn")
pending = assignment.parent / f"submit-{time.time_ns()}.json"
with pending.open("x") as stream:
    json.dump({"command": command, "path": str(verdicts.resolve())}, stream)
print("Scope verified. The supervisor will submit this evidence to Praxis after this turn.")
PY
