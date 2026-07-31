"""``python -m agent_factory.af_clean [path...]`` — the runnable E1 entry point.

``entry.run_e1`` was the engine's front door and ``resolve_scope`` documents ``/af-clean [path...]``,
but nothing turned a command line into that call, so the human path existed only on paper. This is
the missing step, and it stays deliberately thin: argument parsing, toolchain + exemption
resolution, then straight into ``run_e1``. Every judgment stays in the engine.

DRY RUN IS THE DEFAULT. af-clean runs against arbitrary repositories, most of which never went
through af-build, and the cost of the two mistakes is not symmetric: a dry run that should have
applied wastes one command, while an apply that should not have run edits someone's repository.
``--apply`` is the explicit opt-in.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..af_clean_validate import default_runner, run_validation_and_remediation
from .entry import resolve_scope, run_e1
from .exemptions import derive_exemption_manifest
from .applier import apply_findings as _apply_findings
from .producers import default_producer
from .toolchain import detect_toolchain


def _exempt_prefixes(manifest) -> tuple[str, ...]:
    """The repo-relative prefixes the manifest exempts.

    ``ExemptionManifest.paths()`` is a METHOD and ``entries`` is a dict keyed by path, so the
    tolerant ``getattr(...) or getattr(...)`` shape this started as silently picked up the bound
    method and tried to iterate it. Read the documented API directly instead; a guess that
    type-checks at import and fails at runtime is worse than a hard dependency on the real one."""
    paths = manifest.paths()
    return tuple(str(p) for p in paths if str(p).strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="af-clean",
        description="Portable aggressive AI-slop cleanup. Reports located findings; dry by default.")
    ap.add_argument("path", nargs="?", default=None,
                    help="subtree to scope to (default: the whole repo)")
    ap.add_argument("--repo-root", default=".", help="repository root (default: cwd)")
    ap.add_argument("--apply", action="store_true",
                    help="actually apply findings (default is a dry run that changes nothing)")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip the validation+remediation step (it is on by default for E1)")
    ap.add_argument("--skip-verify", action="store_true",
                    help="apply WITHOUT blind verification — unsafe, and says so in the output")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / ".git").exists():
        print(f"af-clean: {repo_root} is not a git repository — refusing to run.", file=sys.stderr)
        return 2

    scope = resolve_scope(repo_root, args.path)
    toolchain = detect_toolchain(repo_root)
    manifest = derive_exemption_manifest(repo_root)
    exempt = _exempt_prefixes(manifest)

    langs = getattr(toolchain, "languages", None) or getattr(toolchain, "toolchains", ()) or ()
    print(f"af-clean: scope={scope}", flush=True)
    print(f"af-clean: languages detected={len(langs)}  exempt paths={len(exempt)}", flush=True)
    print(f"af-clean: mode={'APPLY' if args.apply else 'dry-run (nothing will be written)'}", flush=True)
    if args.apply and args.skip_verify:
        print("af-clean: WARNING — blind verification disabled; edits land on your say-so alone.")

    # --apply now actually applies: gate -> blind verify -> commit stack. Without this the flag was
    # decorative, and the entire back half of the engine was unreachable from the command line.
    applied_outcome: dict = {}

    # The commit stack's post-commit validation (B25) is what truncates the stack when a layer
    # breaks the repo. Passing nothing meant it defaulted to a constant True — the gate existed,
    # was tested, and was wired to "always valid" on the only path a human actually runs. Give it
    # the real validator unless the operator explicitly turned validation off.
    def _validate_layer(path: Path) -> bool:
        return run_validation_and_remediation(path, runner=default_runner).passed

    def _apply(findings):
        applied_outcome["outcome"] = _apply_findings(
            repo_root, findings, skip_verify=args.skip_verify,
            validate_fn=None if args.no_validate else _validate_layer)

    result = run_e1(
        repo_root,
        args.path,
        produce_findings=default_producer(exempt=exempt),
        apply_findings=_apply if args.apply else None,
        dry_run=not args.apply,
        runner=default_runner,
        validate_kwargs=None if not args.no_validate else {"commands": {}},
    )

    findings = list(getattr(result, "findings", ()) or ())
    print(f"\naf-clean: {len(findings)} admitted finding(s)")
    for f in findings[:200]:
        loc = f.location
        where = f"{loc.file}:{loc.line}" if loc else "<unlocated>"
        print(f"  [{f.tier}] {where}  {f.rule}" + (f" — {f.proposal}" if f.proposal else ""))
    if len(findings) > 200:
        print(f"  ... and {len(findings) - 200} more")

    outcome = applied_outcome.get("outcome")
    if outcome is not None:
        print(f"\naf-clean: {outcome.summary()}", flush=True)
        for f, reason in outcome.reported[:20]:
            loc = f.location
            print(f"  reported (not applied) {loc.file}:{loc.line} — {reason}")

    report = getattr(result, "validation", None) or getattr(result, "validation_report", None)
    if report is not None:
        print(f"\naf-clean: validation report: {report}")
    # No pass/fail exit code: E1 is advisory by construction — there is no ticket for a verdict to
    # gate, so a non-zero exit would be inventing one. Only a real error (above) is non-zero.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
