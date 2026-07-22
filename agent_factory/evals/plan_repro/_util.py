"""Shared entrypoint plumbing for the plan_repro eval drivers.

These helpers were duplicated verbatim across ``run_eval_pipeline`` and ``run_eval_subscription``;
they live here so the retry policy, stream encoding, and .env loading have a single source.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable


def force_utf8_streams() -> None:
    """Force UTF-8 stdout/stderr so feature text with chars like U+2265 ('≥') never crashes a
    Windows cp1252 console. Best-effort — a stream without ``reconfigure`` is left as-is."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def load_repo_dotenv(root: Path) -> None:
    """Load ``root/.env`` into the environment without overriding already-set vars."""
    env = root / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def retrying(base: Callable[[str], str], *, attempts: int = 3) -> Callable[[str], str]:
    """Wrap a ``complete(prompt)->str`` backend so a hung/transient CLI call is retried a few times
    before the exception propagates (a single 600s hang must not kill the whole scoring pass)."""
    def complete(prompt: str) -> str:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return base(prompt)
            except Exception as exc:  # noqa: BLE001 — TimeoutExpired or transient CLI error
                last = exc
                print(f"  [judge retry {attempt + 1}/{attempts} after {type(exc).__name__}]",
                      flush=True)
        raise last  # type: ignore[misc]
    return complete
