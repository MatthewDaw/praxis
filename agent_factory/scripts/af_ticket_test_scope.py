"""Reject unscoped ticket test launches without manufacturing a test result."""
from pathlib import Path
import re


def rejection_reason(args: list[str], *, allow_project_types: bool = False) -> str:
    """Require concrete selectors for generic suite runners.

    Project-specific acceptance scripts remain responsible for their own scope.
    Call this before resource admission, so rejected commands never hold a slot.
    """
    if not args:
        return ''
    command = Path(args[0]).name
    words = args[1:]
    type_check = command.startswith(('tsc', '_tsc')) or (
        command in {'npm', 'npm-cli.js', 'npx', 'npx-cli.js'} and 'typecheck' in words
    )
    if type_check and not allow_project_types:
        selected_types = any(re.search(r'\.[cm]?tsx?$', x) for x in words)
        selected_types = selected_types or any(
            re.search(r'tsconfig[.-](?:ticket|r\d+).*\.json$', Path(x).name, re.I) for x in words
        )
        if not selected_types:
            return 'Use the scoped type helper with the ticket base; whole-project type censuses are disabled for ticket checks.'
    if 'test:parity' in words:
        return 'Legacy host parity is outside this league ticket build.'
    vitest = command.startswith('vitest')
    if vitest and ('related' in words or any(x.startswith('--changed') for x in words)):
        return 'Use explicit affected test files; shared application imports expand into unrelated suites.'
    generic = vitest or command.startswith('playwright') or any(
        x in {'test:integration', 'test:e2e'} for x in words
    )
    if command in {'npm', 'npm-cli.js', 'npx', 'npx-cli.js'}:
        generic = generic or 'test' in words or 't' in words or 'test:ticket' in words
    if not generic:
        return ''
    selected = any(re.search(r'\.(?:test|itest|spec)\.[cm]?[jt]sx?$', x) for x in words)
    selected = selected or any(x.startswith(('src/features/league/', 'src/features/forms/')) for x in words)
    # A dedicated browser config can define its own narrowly selected journey.
    config = next((x for x in words if 'playwright.' in x and x.endswith(('.js', '.ts', '.mjs'))), '')
    selected = selected or bool(config and Path(config).name not in {
        'playwright.config.js', 'playwright.config.ts', 'playwright.parity.config.js'
    })
    return '' if selected else 'Select explicit affected test files/cases; whole-suite ticket censuses are disabled.'
