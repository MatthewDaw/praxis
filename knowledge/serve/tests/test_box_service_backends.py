"""R88 acceptance: view/switch the box's active model backend.

The backend-switch mechanism is pure decision logic with no subprocess/I-O
beyond the file read/write injectable through a temp path, so it is
unit-testable with no live box service. The exclusivity guarantee (only the
selected backend's credential is exposed) is asserted through the credential
var mapping.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from knowledge.serve.box_service_backends import (
    DEFAULT_BACKEND_FILE,
    VALID_BACKENDS,
    BACKEND_CREDENTIAL_VAR,
    _backend_file,
    backend_session_credential,
    read_active_backend,
    write_active_backend,
)


@pytest.fixture
def temp_backend_file(monkeypatch):
    """Redirect the backend file to a temp path for each test."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "backend"
        monkeypatch.setenv("PRAXIS_BACKEND_FILE", str(p))
        yield str(p)


# -- write_active_backend ---------------------------------------------------


def test_write_and_read_backend(temp_backend_file):
    write_active_backend("sonnet")
    assert read_active_backend() == "sonnet"


def test_write_normalizes_case_and_whitespace(temp_backend_file):
    write_active_backend("  Deepseek  ")
    assert read_active_backend() == "deepseek"


def test_write_refuses_invalid_backend(temp_backend_file):
    with pytest.raises(ValueError, match="invalid backend"):
        write_active_backend("grok")


def test_write_creates_parent_directory(temp_backend_file, monkeypatch):
    nested = os.path.join(temp_backend_file, "subdir", "backend")
    monkeypatch.setenv("PRAXIS_BACKEND_FILE", nested)
    write_active_backend("sonnet")
    assert os.path.isfile(nested)
    with open(nested) as fh:
        assert fh.read().strip() == "sonnet"


# -- read_active_backend ----------------------------------------------------


def test_read_absent_file_raises(temp_backend_file):
    # temp_backend_file is a path that doesn't exist yet
    with pytest.raises(FileNotFoundError, match="no active backend set"):
        read_active_backend()


def test_read_bogus_value_raises(temp_backend_file):
    Path(temp_backend_file).parent.mkdir(parents=True, exist_ok=True)
    Path(temp_backend_file).write_text("garbage")
    with pytest.raises(ValueError, match="unknown backend"):
        read_active_backend()


# -- credential exclusivity ------------------------------------------------


def test_every_valid_backend_has_a_credential_mapping():
    """Every VALID_BACKENDS entry must map to a credential env var, so the session
    launcher always knows which env var to expose for its backend."""
    for backend in VALID_BACKENDS:
        assert backend in BACKEND_CREDENTIAL_VAR, (
            f"missing credential mapping for {backend!r}"
        )
        assert BACKEND_CREDENTIAL_VAR[backend], (
            f"empty credential var name for {backend!r}"
        )


def test_credential_vars_are_distinct():
    """No two backends may share the same credential var — the exclusivity guarantee
    requires exactly one credential per backend."""
    seen: dict[str, str] = {}
    for backend, var in BACKEND_CREDENTIAL_VAR.items():
        assert var not in seen, (
            f"credential var {var!r} mapped to both {seen[var]!r} and {backend!r}"
        )
        seen[var] = backend


# -- default path -----------------------------------------------------------


def test_default_backend_file_is_under_home():
    assert DEFAULT_BACKEND_FILE.startswith(os.path.expanduser("~"))
    assert DEFAULT_BACKEND_FILE.endswith("backend")


# -- backend_session_credential --------------------------------------------


def test_credential_env_returns_empty_when_no_file(temp_backend_file):
    """When no backend file exists, the injection is empty — the session
    launches without a credential rather than fabricating one."""
    assert backend_session_credential() == {}


def test_credential_env_returns_empty_when_file_has_bogus_value(temp_backend_file):
    Path(temp_backend_file).parent.mkdir(parents=True, exist_ok=True)
    Path(temp_backend_file).write_text("xanadu")
    assert backend_session_credential() == {}


def test_credential_env_returns_empty_when_credential_not_in_env(monkeypatch, temp_backend_file):
    """The backend file says 'sonnet' but ANTHROPIC_API_KEY is unset — the box
    was not provisioned for that backend, so nothing is exposed."""
    write_active_backend("sonnet")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert backend_session_credential() == {}


def test_credential_env_exposes_selected_backends_credential(monkeypatch, temp_backend_file):
    """When sonnet is active AND ANTHROPIC_API_KEY is set, only that credential
    appears in the session injection."""
    write_active_backend("sonnet")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cred = backend_session_credential()
    assert cred == {"ANTHROPIC_API_KEY": "sk-ant-secret"}


def test_credential_env_never_exposes_both(monkeypatch, temp_backend_file):
    """Even when BOTH credentials happen to be set in the box service's env
    (a misconfiguration), only the active one is exposed — the exclusivity
    guarantee holds."""
    write_active_backend("deepseek")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    cred = backend_session_credential()
    assert cred == {"OPENROUTER_API_KEY": "sk-or-secret"}
    assert "ANTHROPIC_API_KEY" not in cred


def test_credential_env_empty_when_selected_credential_is_empty(monkeypatch, temp_backend_file):
    """The credential var exists but is empty — treated as absent."""
    write_active_backend("sonnet")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert backend_session_credential() == {}


def test_credential_env_with_default_backend_override(monkeypatch, temp_backend_file):
    """The ``default_backend`` override bypasses the file entirely."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    cred = backend_session_credential(default_backend="sonnet")
    assert cred == {"ANTHROPIC_API_KEY": "sk-ant-secret"}
