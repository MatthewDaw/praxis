#!/usr/bin/env python3
"""Factory setup doctor — one command that verifies EVERY dependency the af-build hook + MCP paths
need, and reports PASS/FAIL with the exact fix for each missing piece.

Standing up af-build on a fresh box is painful because SO many independent things must line up at
once — the Postgres DB the server reads, the HTTP API, the hook's OWN Cognito auth (a separate client
from the MCP tools), the identity cache, the active org — and when one is missing nothing tells you
WHICH. This command is that missing signpost:

    python -m agent_factory.tools.doctor [project]

It checks, and names precisely what to fix for each:
  * DB reachable            — the Postgres the serve process reads (PRAXIS_DB_URL / secret).
  * HTTP API + hook auth     — the hook can mint a token AND reach the API (the path that failed
                               silently); delegates to ``hooks/_praxis.preflight`` for the pinpoint.
  * identity cache present   — ``~/.praxis/<cache>.json`` (or PRAXIS_MCP_CACHE) with a refresh token.
  * PRAXIS_ORG resolves      — the active org and where it came from (pin > cache > default).
  * MCP org == hook org      — the ONE hard tenancy rule: the praxis_* tools and the hook must agree.
  * plugin wired             — best-effort detection that the agent-factory plugin/hook is installed.
  * project tickets resolve  — (if a project is given) a live incomplete-set read end to end.

Exit code is non-zero iff any REQUIRED check failed, so it doubles as a setup gate in a script.
Unlike the Stop hook, the doctor runs inside the praxis venv, so it can exercise BOTH the stdlib hook
client (``hooks/_praxis``) AND the real MCP identity module (``knowledge.mcp.identity``) and confirm
they resolve the same org — the split that motivated this whole tool.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NamedTuple, Optional

# Import the SAME stdlib hook client af-build's Stop gate uses (mirror resolve_preview's path insert),
# so "the hook path" the doctor checks is byte-for-byte the one that runs in production.
_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402


class Check(NamedTuple):
    name: str
    ok: Optional[bool]        # True=pass, False=fail, None=skipped/advisory (never fails the run)
    detail: str               # what was found
    fix: str = ""             # how to fix it (shown only on fail)
    required: bool = True     # a failed non-required check warns but does not set the exit code


# --------------------------------------------------------------------------- individual checks

def check_db() -> Check:
    """Can the serve process reach its Postgres? (The MCP + HTTP paths ultimately read it.)"""
    try:
        from knowledge.serve import db
    except Exception as exc:  # noqa: BLE001
        return Check("DB reachable", None, f"cannot import knowledge.serve.db ({exc})",
                     required=False)
    dsn = db.resolve_dsn()
    if not dsn:
        return Check("DB reachable", False, "no DSN resolvable",
                     "set PRAXIS_DB_URL in praxis/.env (or configure PRAXIS_DB_SECRET + AWS creds).")
    try:
        conn = db.connect(dsn)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return Check("DB reachable", False, f"connect/query failed: {exc}",
                     "confirm Postgres is up and PRAXIS_DB_URL points at it.")
    return Check("DB reachable", True, "connected + SELECT 1 ok")


def check_hook_path(pf: "_praxis.PreflightResult") -> list[Check]:
    """The hook path (identity cache + Cognito mint + HTTP API), from the shared preflight verdict."""
    cache = _praxis._cache_path()
    cache_ok = cache.exists()
    checks = [Check("identity cache present", cache_ok, str(cache),
                    "log in via the praxis_login MCP tool to create it, or set PRAXIS_API_KEY / "
                    "PRAXIS_MCP_CACHE.")]
    if pf.ok:
        checks.append(Check("HTTP API + hook auth", True,
                            f"mint + authenticated probe ok at {pf.api_base}"))
    else:
        checks.append(Check(
            "HTTP API + hook auth", False,
            f"{pf.kind}: " + "; ".join(pf.failures),
            "fix the item(s) above — " + ("this is a config error and will not self-heal by retrying."
                                          if pf.kind == "misconfig" else "bring the server up.")))
    return checks


def check_org(pf: "_praxis.PreflightResult") -> Check:
    """PRAXIS_ORG resolution — the active org and its source (pin > cache > default)."""
    warn = pf.org_source == "default"
    return Check("PRAXIS_ORG resolves", None if warn else True,
                 f"org='{pf.org}' (via {pf.org_source})",
                 "pin PRAXIS_ORG or run praxis_select_org if the default org is wrong for this "
                 "project." if warn else "",
                 required=False)


def check_org_agreement(pf: "_praxis.PreflightResult") -> Check:
    """THE one hard tenancy rule: the MCP tools and the hook must resolve the SAME org, or the hook
    reads a different tenant's tickets than the tools wrote."""
    try:
        from knowledge.mcp import identity
    except Exception as exc:  # noqa: BLE001
        return Check("MCP org == hook org", None,
                     f"MCP identity module unavailable ({exc}); cannot cross-check", required=False)
    try:
        mcp_org = identity.active_org()
    except Exception as exc:  # noqa: BLE001
        return Check("MCP org == hook org", None,
                     f"MCP org unresolved ({exc}) — likely not logged in; skipped", required=False)
    if mcp_org == pf.org:
        return Check("MCP org == hook org", True, f"both resolve '{pf.org}'")
    return Check("MCP org == hook org", False,
                 f"MCP='{mcp_org}' but hook='{pf.org}'",
                 "align them: pin the same PRAXIS_ORG for both, or run praxis_select_org. The hook "
                 "and the praxis_* tools MUST operate in one org.")


def check_plugin() -> Check:
    """Best-effort: is the agent-factory plugin/hook actually wired? (Advisory — never fails the run.)"""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    hooks_json = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"
    found: list[str] = []
    if root:
        found.append(f"CLAUDE_PLUGIN_ROOT={root}")
    if hooks_json.is_file():
        found.append(f"hooks.json at {hooks_json}")
    # A marketplace install typically lands under ~/.claude/plugins.
    plugins_dir = Path.home() / ".claude" / "plugins"
    if plugins_dir.is_dir():
        hits = [str(p) for p in plugins_dir.rglob("hooks.json") if "agent_factory" in str(p).lower()
                or "agent-factory" in str(p).lower()]
        if hits:
            found.append(f"installed: {hits[0]}")
    if found:
        return Check("plugin wired", None, "; ".join(found), required=False)
    return Check("plugin wired", None,
                 "no plugin install detected (CLAUDE_PLUGIN_ROOT unset, no ~/.claude/plugins entry)",
                 "if the Stop hook never fires, install/enable the agent-factory plugin so hooks.json "
                 "is loaded.", required=False)


def check_project(project: str) -> Check:
    """End-to-end: read the project's incomplete ticket set through the hook client (proves auth +
    org + snapshot all line up for a real query)."""
    try:
        import _ticket_state as ts
        ref = ts.project_ref(project)
        space, snapshot = ref.plan
        items = _praxis.incomplete_requirements(ref.plan[0], space=space, snapshot=snapshot)
    except _praxis.PraxisUnreachable as exc:
        return Check(f"project '{project}' tickets resolve", False, f"read failed: {exc}",
                     "fix the hook-auth / DB failures above, then re-run.")
    except Exception as exc:  # noqa: BLE001
        return Check(f"project '{project}' tickets resolve", False, f"error: {exc}",
                     "check the project name (bare, e.g. team-app) and that its prd- snapshot exists.")
    return Check(f"project '{project}' tickets resolve", True,
                 f"read {len(items)} incomplete ticket(s) from {space}:{snapshot}")


# --------------------------------------------------------------------------- report

def _render(checks: list[Check]) -> tuple[str, bool]:
    """Render the PASS/FAIL/WARN report; return (text, all_required_passed)."""
    lines: list[str] = []
    ok_all = True
    for c in checks:
        if c.ok is True:
            tag = "PASS"
        elif c.ok is False:
            tag = "FAIL"
            if c.required:
                ok_all = False
        else:
            tag = "WARN"
        lines.append(f"  [{tag}] {c.name}: {c.detail}")
        if c.ok is not True and c.fix:
            lines.append(f"         fix: {c.fix}")
    return "\n".join(lines), ok_all


def run(project: str | None) -> int:
    # One live preflight drives the hook-path, org, and agreement checks (fresh — no cache).
    pf = _praxis.preflight(live=True, use_cache=False)

    checks: list[Check] = [check_db()]
    checks += check_hook_path(pf)
    checks += [check_org(pf), check_org_agreement(pf), check_plugin()]
    if project:
        checks.append(check_project(project))

    report, ok_all = _render(checks)
    print(f"factory doctor — api={pf.api_base}\n")
    print(report)
    print()
    if ok_all:
        print("RESULT: all required checks PASS — af-build's Praxis path is ready.")
        return 0
    print("RESULT: FAIL — fix the items marked FAIL above (WARN items are advisory). "
          "Re-run `python -m agent_factory.tools.doctor` to confirm.")
    return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m agent_factory.tools.doctor",
        description="Verify every af-build setup dependency (DB, HTTP API, hook auth, identity cache, "
                    "org, plugin) and report PASS/FAIL with the exact fix for each.")
    p.add_argument("project", nargs="?", default=None,
                   help="optional bare project name (e.g. team-app) — adds a live end-to-end "
                        "incomplete-ticket read to prove the whole path resolves.")
    args = p.parse_args(argv)
    return run(args.project)


if __name__ == "__main__":
    raise SystemExit(main())
