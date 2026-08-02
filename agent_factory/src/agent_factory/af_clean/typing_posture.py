"""Deterministic detections about a repo's typing/lint POSTURE — reported, never edited.

The motivating incident: ``mypy --strict`` on a backend was believed to have 74 errors. The true
number was 2261. The gap was one missing ``__init__.py`` — a relative import mypy could not resolve
made it print ``errors prevented further checking`` and STOP, having analysed a fraction of the
tree. The subtotal from that aborted run was quoted as a total and consumed by a build gate as if it
were one.

The generalisable failure is not about mypy: **a checker that did not run is indistinguishable from
a checker that passed.** ``tsc`` bails on config errors, pytest collection errors report ``0
failed``, eslint silently skips unparseable files. Nothing asks "did the tool actually finish?"

Three detections answer that, plus the JS position:

1. :func:`detect_checker_abort` — the checker stopped early. Reports the marker verbatim and the
   counts, and this is the one that would have caught 74-vs-2261 on day one.
2. :func:`detect_unenforced_checker` — a checker is configured but nothing runs it, or the config
   documents one gate command while CI runs a different one (that mismatch alone failed a build
   round in the motivating repo).
3. :func:`detect_missing_checker` — an ecosystem that supports a checker has none configured.
4. :func:`detect_new_javascript` — a NEW ``.js``/``.jsx`` file in a repo that already has
   TypeScript configured.

**None of these edit anything.** Every finding here carries ``change_class="report-only"``: turning
a checker on, flipping ``strict``, wiring CI, or bulk-converting a language are repo-wide POLICY
decisions producing unbounded work — flipping ``strict`` in the motivating repo produced 2141 errors
across 133 files. af-clean satisfies the gate the repo chose; it does not choose the gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .findings import CLASS_REPORT_ONLY, Finding, Location

# Literal strings a checker prints when it gives up part-way. Each is quoted verbatim in the
# finding, because "the tool aborted" is a claim the reader must be able to check against the log
# rather than take on trust.
ABORT_MARKERS: tuple[str, ...] = (
    "errors prevented further checking",          # mypy: unresolved import, stops the whole run
    "error TS5083",                               # tsc: cannot read tsconfig
    "error TS6053",                               # tsc: input file not found
    "Cannot read file",                           # tsc: config/include resolution failure
    "ERROR collecting",                           # pytest: collection error, then reports 0 failed
    "Interrupted: ",                              # pytest: collection aborted the session
    "Oops! Something went wrong",                 # eslint: config/parse failure
    "Parsing error:",                             # eslint: a file it could not read at all
    "SyntaxError",                                # ruff/flake8/py compile: file skipped, not judged
)

#: Below this ratio of analysed-to-census files, a run that printed no marker is still treated as
#: having aborted. A checker that looked at a fifth of the tree has not judged the tree, and the
#: 74-vs-2261 case reported no proportion at all — only a count that looked reasonable in isolation.
PLAUSIBLE_COVERAGE_RATIO = 0.8


@dataclass(frozen=True)
class CheckerRun:
    """One checker invocation's observable result — what a caller can see WITHOUT trusting it.

    ``files_analysed`` is what the tool itself claims to have looked at (mypy's "Checked N source
    files", tsc's file count, pytest's collected count); ``None`` when the tool reported no such
    number, which is itself a reason the count cannot be validated.
    """

    tool: str
    command: str
    output: str
    exit_code: int = 0
    files_analysed: int | None = None
    config_path: str = ""


def _report(rule: str, file: str, line: int, proposal: str) -> Finding:
    """A located, report-only finding. Advise tier: posture is never auto-actioned."""
    return Finding(
        rule=rule,
        tier="advise",
        location=Location(file=file, line=line),
        change_class=CLASS_REPORT_ONLY,
        proposal=proposal,
    )


def found_abort_markers(output: str) -> tuple[str, ...]:
    """Every abort marker present in ``output``, in the order :data:`ABORT_MARKERS` declares them."""
    return tuple(m for m in ABORT_MARKERS if m in (output or ""))


def detect_checker_abort(run: CheckerRun, census_file_count: int) -> Finding | None:
    """The checker did not finish: it printed an abort marker, or it analysed implausibly few files.

    Returns ``None`` when the run looks complete. A run that DID abort is reported even when it
    listed few errors — especially then, since a short error list from an aborted checker is exactly
    the signal that gets mistaken for a clean bill of health.
    """
    markers = found_abort_markers(run.output)
    analysed = run.files_analysed
    implausible = (
        analysed is not None
        and census_file_count > 0
        and analysed < census_file_count * PLAUSIBLE_COVERAGE_RATIO
    )
    if not markers and not implausible:
        return None

    why = []
    if markers:
        why.append("output contains " + ", ".join(repr(m) for m in markers))
    if implausible:
        why.append(
            f"analysed {analysed} file(s) against a census of {census_file_count} "
            f"(< {PLAUSIBLE_COVERAGE_RATIO:.0%})"
        )
    elif analysed is not None:
        why.append(f"analysed {analysed} file(s) against a census of {census_file_count}")
    else:
        why.append(f"reported no file count; census is {census_file_count}")

    return _report(
        "checker-aborted-early",
        run.config_path or "<repo>",
        1,
        f"{run.tool} DID NOT COMPLETE ({'; '.join(why)}) — `{run.command}` exited {run.exit_code}. "
        "Any error count from this run is a SUBTOTAL of an aborted analysis, not a total, and must "
        "not be consumed as a gate result. Fix what stopped the tool, then re-measure.",
    )


# Where a repo declares its checkers, and the marker that proves the declaration is really there.
# Keyed by (config file, needle) so a shared file like pyproject.toml can host several checkers.
_PYTHON_CONFIGS: tuple[tuple[str, str, str], ...] = (
    ("mypy", "pyproject.toml", "[tool.mypy]"),
    ("mypy", "mypy.ini", ""),
    ("mypy", "setup.cfg", "[mypy]"),
    ("ruff", "pyproject.toml", "[tool.ruff]"),
    ("ruff", "ruff.toml", ""),
    ("ruff", ".ruff.toml", ""),
    ("flake8", ".flake8", ""),
    ("flake8", "setup.cfg", "[flake8]"),
    ("pyright", "pyrightconfig.json", ""),
)

_JS_CONFIGS: tuple[tuple[str, str, str], ...] = (
    ("tsc", "tsconfig.json", ""),
    ("eslint", ".eslintrc", ""),
    ("eslint", ".eslintrc.json", ""),
    ("eslint", ".eslintrc.js", ""),
    ("eslint", ".eslintrc.cjs", ""),
    ("eslint", "eslint.config.js", ""),
    ("eslint", "eslint.config.mjs", ""),
)

# Where a repo would INVOKE a checker if it enforced one. Read as text and searched for the tool
# name: a workflow that mentions `mypy` anywhere is running it, and one that never does is not.
_ENFORCEMENT_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml",
    "Makefile",
    "justfile",
    "Justfile",
    "package.json",
    "azure-pipelines.yml",
    ".circleci/config.yml",
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _configured_checkers(root: Path, specs: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """``{tool: config path}`` for every checker this repo actually declares."""
    found: dict[str, str] = {}
    for tool, filename, needle in specs:
        if tool in found:
            continue
        path = root / filename
        if not path.exists():
            continue
        if needle and needle not in _read(path):
            continue
        found[tool] = filename
    return found


def _enforcement_sites(root: Path, tool: str) -> list[tuple[str, str]]:
    """``(path, line)`` pairs where something invokes ``tool``."""
    sites: list[tuple[str, str]] = []
    for pattern in _ENFORCEMENT_GLOBS:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            for raw in _read(path).splitlines():
                if tool in raw:
                    sites.append((str(path.relative_to(root)), raw.strip()))
    return sites


# A documented gate command inside a config comment, e.g. "# gate: mypy appeal_api". The mismatch
# this catches is subtle and real: pyproject documented `mypy appeal_api` while CI ran
# `mypy --strict .`, and only the wider command ever failed.
_DOCUMENTED_COMMAND = re.compile(r"(?:^|\s)((?:mypy|ruff|flake8|pyright|tsc|eslint)\s+[^\"'\n]+)")


def _documented_commands(text: str, tool: str) -> list[str]:
    return [
        m.group(1).strip()
        for m in _DOCUMENTED_COMMAND.finditer(text)
        if m.group(1).split()[0] == tool
    ]


def detect_unenforced_checker(repo_root: "str | Path") -> list[Finding]:
    """A checker is configured but nothing runs it — or runs it with a DIFFERENT command.

    Two findings, because they fail differently. An unenforced checker is a gate nobody stands at.
    A mismatched command is worse: everyone believes the documented, narrower command is the gate,
    while CI quietly enforces a wider one (or the reverse), so the number people quote and the
    number that blocks a build are measuring different things.
    """
    root = Path(repo_root)
    out: list[Finding] = []
    configured = {**_configured_checkers(root, _PYTHON_CONFIGS),
                  **_configured_checkers(root, _JS_CONFIGS)}

    for tool, config in sorted(configured.items()):
        sites = _enforcement_sites(root, tool)
        if not sites:
            out.append(_report(
                "checker-configured-but-not-enforced", config, 1,
                f"{tool} is configured in {config} but no CI workflow, hook, or documented gate "
                f"command invokes it — searched {', '.join(_ENFORCEMENT_GLOBS)}. Wiring it is a "
                "human decision; af-clean does not add CI.",
            ))
            continue

        documented = set(_documented_commands(_read(root / config), tool))
        invoked = {cmd for _p, line in sites for cmd in _documented_commands(line, tool)}
        divergent = sorted(documented - invoked)
        if documented and invoked and divergent:
            out.append(_report(
                "checker-gate-command-mismatch", config, 1,
                f"{config} documents `{divergent[0]}` for {tool}, but the enforced command is "
                f"`{sorted(invoked)[0]}` ({sites[0][0]}). The documented gate and the real gate are "
                "measuring different things.",
            ))
    return out


# An ecosystem, the manifest that proves it is present, and the checkers it supports. Go and Rust
# are absent on purpose: their compilers type-check unconditionally, so there is no such thing as an
# unconfigured type gate there.
_ECOSYSTEMS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    ("python", ("pyproject.toml", "setup.py", "requirements.txt"),
     ("mypy", "pyright"), ("ruff", "flake8")),
    ("javascript", ("package.json",), ("tsc",), ("eslint",)),
)


def detect_missing_checker(repo_root: "str | Path") -> list[Finding]:
    """An ecosystem that supports a type or lint gate has none configured. Reported, never installed."""
    root = Path(repo_root)
    out: list[Finding] = []
    configured = {**_configured_checkers(root, _PYTHON_CONFIGS),
                  **_configured_checkers(root, _JS_CONFIGS)}

    for ecosystem, manifests, type_gates, lint_gates in _ECOSYSTEMS:
        manifest = next((m for m in manifests if (root / m).exists()), None)
        if manifest is None:
            continue
        for kind, gates in (("type checker", type_gates), ("linter", lint_gates)):
            if any(g in configured for g in gates):
                continue
            out.append(_report(
                "no-checker-configured", manifest, 1,
                f"{ecosystem} project with no {kind} configured (looked for "
                f"{', '.join(gates)}). Introducing one is a project decision with unbounded "
                "follow-on work; af-clean reports it and installs nothing.",
            ))
    return out


# Files a repo legitimately keeps as .js even under TypeScript: build/tool configs that their own
# loader requires as JavaScript. Converting these breaks the tool, so they are never findings.
_JS_CONFIG_BASENAMES = frozenset({
    "eslint.config.js", "eslint.config.mjs", ".eslintrc.js", ".eslintrc.cjs",
    "tailwind.config.js", "postcss.config.js", "next.config.js", "vite.config.js",
    "jest.config.js", "babel.config.js", "rollup.config.js", "webpack.config.js",
    "commitlint.config.js", "prettier.config.js", "metro.config.js",
})

_JS_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs"})


def detect_new_javascript(
    repo_root: "str | Path",
    added_paths: Sequence[str],
    *,
    exempt: Iterable[str] = (),
) -> list[Finding]:
    """NEW ``.js``/``.jsx`` files added to a repo that already has TypeScript configured.

    Enforcement is deliberately ASYMMETRIC. A newly added JavaScript file in a TypeScript repo is a
    located, unambiguous finding and cheap to fix — catching it stops the problem growing. Converting
    an EXISTING ``.js`` file is a migration, not a cleanup: it changes module resolution, build
    inputs, and often surfaces latent type errors across the import graph, so it belongs in a project
    ticket with its own acceptance, one file at a time under the per-file conversion verifier. And a
    repo with no TypeScript at all gets nothing from here — introducing a TS toolchain is a project
    decision, exactly like turning on a checker that is not configured.
    """
    root = Path(repo_root)
    if not (root / "tsconfig.json").exists():
        return []

    exempt_prefixes = tuple(str(e).strip("/") for e in exempt if str(e).strip())
    out: list[Finding] = []
    for rel in added_paths:
        rel = str(rel).strip()
        if not rel or Path(rel).suffix not in _JS_SUFFIXES:
            continue
        if Path(rel).name in _JS_CONFIG_BASENAMES:
            continue
        if any(rel == p or rel.startswith(p + "/") for p in exempt_prefixes):
            continue
        out.append(_report(
            "new-javascript-in-typescript-repo", rel, 1,
            f"{rel} is a new JavaScript file in a repo that already configures TypeScript "
            "(tsconfig.json). New code should be TypeScript with strict typing unless there is a "
            "strong reason otherwise; converting it now is cheap, converting it later is a migration.",
        ))
    return out


def typing_posture_findings(
    repo_root: "str | Path",
    *,
    checker_runs: Sequence[CheckerRun] = (),
    census_file_count: int = 0,
    added_paths: Sequence[str] = (),
    exempt: Iterable[str] = (),
) -> list[Finding]:
    """Every posture finding for ``repo_root``, in reporting order.

    ``checker_runs`` is empty on a run that did not execute any checker — the abort detection then
    contributes nothing rather than guessing, because "no run" and "an aborted run" are different
    claims and only one of them is evidence.
    """
    out: list[Finding] = []
    for run in checker_runs:
        finding = detect_checker_abort(run, census_file_count)
        if finding is not None:
            out.append(finding)
    out.extend(detect_unenforced_checker(repo_root))
    out.extend(detect_missing_checker(repo_root))
    out.extend(detect_new_javascript(repo_root, added_paths, exempt=exempt))
    return out
