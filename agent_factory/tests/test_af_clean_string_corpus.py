"""R6 — af-clean's B6/B20 string-dispatch corpus and whole-token quarantine, plus the S3
secret-redaction guard on corpus/report/ledger output.

Acceptance: the interpolated path to build_completeness_gate.py quarantines
build_completeness_gate; the token 'run' appearing only in a log string quarantines nothing;
and given an API key literal and a .env file no corpus entry, report line or ledger entry
contains the key value.
"""

from __future__ import annotations

from agent_factory.af_clean_string_corpus import (
    build_corpus,
    format_report_line,
    ledger_entry,
    quarantines,
)


def test_interpolated_path_quarantines_the_named_module(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hooks.json").write_text('{"command": "not python, ignored"}', encoding="utf-8")
    (repo / "runner.py").write_text(
        'CMD = f"${{CLAUDE_PLUGIN_ROOT}}/hooks/build_completeness_gate.py"\n',
        encoding="utf-8",
    )
    # build_completeness_gate itself has zero importers anywhere in this repo.
    (repo / "build_completeness_gate.py").write_text("def main():\n    pass\n", encoding="utf-8")

    corpus = build_corpus(repo)

    assert quarantines("build_completeness_gate", corpus)


def test_token_only_in_log_message_is_not_quarantined(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "worker.py").write_text(
        'import logging\n'
        'logger = logging.getLogger(__name__)\n'
        'def do_work():\n'
        '    logger.info("run completed successfully")\n',
        encoding="utf-8",
    )

    corpus = build_corpus(repo)

    assert not quarantines("run", corpus)


def test_api_key_never_reaches_corpus_report_or_ledger(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    (repo / ".env").write_text(f"API_KEY={secret}\n", encoding="utf-8")
    (repo / "config.py").write_text(f'DEFAULT_KEY = "{secret}"\n', encoding="utf-8")

    corpus = build_corpus(repo)

    assert not any(secret in entry.raw for entry in corpus)
    for entry in corpus:
        assert secret not in format_report_line(entry)
        assert secret not in str(ledger_entry("DEFAULT_KEY", entry))
