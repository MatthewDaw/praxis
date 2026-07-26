"""Acceptance test for ticket R54 (origin allowlist governance, b294d7e1):

given an agent or session attempting to add an origin to the allowlist, the
write is refused; given an unreadable allowlist store, dispatch refuses
rather than proceeding; given the operator using the documented path, the
origin is added.

The allowlist is provisioned out-of-band by the operator only: it is not
writable by any MCP tool, dispatching agent, or box-side session, and
dispatch reads it read-only and fails closed when it cannot be read. The
single documented exception to zero-onboarding is registering one origin per
new repo via the operator CLI (``scripts/manage_origin_allowlist.py``).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from knowledge.serve.dispatch import OriginNotAllowedError, build_dispatch_payload
from knowledge.serve.origin_allowlist import AllowlistUnreadableError, load_allowlist

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ORIGIN_ALLOWLIST_SRC = (
    Path(__file__).resolve().parent.parent / "origin_allowlist.py"
).read_text(encoding="utf-8")
_MANAGE_CLI = _REPO_ROOT / "scripts" / "manage_origin_allowlist.py"


def make_bare_repo(tmp_path):
    repo = tmp_path / "target-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


class TestNoWriteCapabilityInTheSharedModule:
    """Given an agent or dispatching session attempting to add an origin to
    the allowlist, the write is refused: the module every MCP tool /
    dispatching agent / box-side session would import to read the allowlist
    (``origin_allowlist``) structurally has no function to call to mutate it
    at all."""

    def test_origin_allowlist_module_exposes_no_write_function(self):
        tree = ast.parse(_ORIGIN_ALLOWLIST_SRC)
        top_level_funcs = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        write_like = {
            name for name in top_level_funcs
            if any(verb in name for verb in ("add", "remove", "write", "save", "delete", "set"))
        }
        assert write_like == set(), (
            f"origin_allowlist.py must expose no write capability, found: {write_like}"
        )

    def test_origin_allowlist_module_has_no_attribute_to_add_an_origin(self):
        import knowledge.serve.origin_allowlist as origin_allowlist

        for verb in ("add_origin", "remove_origin", "write_allowlist", "save_allowlist"):
            assert not hasattr(origin_allowlist, verb), (
                f"an agent importing origin_allowlist must not find {verb!r}"
            )

    def test_dispatch_module_imports_no_write_capability_either(self):
        """dispatch.py only ever validates against an allowlist it is handed
        (``allowed_origins``) — it never IMPORTS anything from
        ``origin_allowlist`` (let alone a write path), so a dispatching agent
        cannot reach a mutation through the dispatch surface either. (The
        module docstring may still reference ``origin_allowlist.py`` in
        prose, hence walking real import nodes rather than a substring
        check.)"""
        dispatch_src = (Path(__file__).resolve().parent.parent / "dispatch.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(dispatch_src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        assert not any("origin_allowlist" in n for n in names)


class TestFailClosedOnUnreadableStore:
    """Given an unreadable allowlist store, dispatch refuses rather than
    proceeding."""

    def test_missing_file_raises_allowlist_unreadable(self, tmp_path):
        with pytest.raises(AllowlistUnreadableError):
            load_allowlist(tmp_path / "does-not-exist.json")

    def test_unparseable_json_raises_allowlist_unreadable(self, tmp_path):
        store = tmp_path / "allowlist.json"
        store.write_text("not json at all {{{", encoding="utf-8")
        with pytest.raises(AllowlistUnreadableError):
            load_allowlist(store)

    def test_non_array_json_raises_allowlist_unreadable(self, tmp_path):
        store = tmp_path / "allowlist.json"
        store.write_text(json.dumps({"origin": "git@github.com:acme/widgets.git"}), encoding="utf-8")
        with pytest.raises(AllowlistUnreadableError):
            load_allowlist(store)

    def test_non_string_entries_raise_allowlist_unreadable(self, tmp_path):
        store = tmp_path / "allowlist.json"
        store.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(AllowlistUnreadableError):
            load_allowlist(store)

    def test_permission_denied_raises_allowlist_unreadable(self, tmp_path):
        store = tmp_path / "allowlist.json"
        store.write_text(json.dumps(["git@github.com:acme/widgets.git"]), encoding="utf-8")
        store.chmod(0o000)
        try:
            with pytest.raises(AllowlistUnreadableError):
                load_allowlist(store)
        finally:
            store.chmod(0o644)  # restore so tmp_path cleanup can remove it

    def test_dispatch_never_proceeds_when_the_store_load_raises(self, tmp_path):
        """The unreadable-store failure happens before ``build_dispatch_payload``
        is even reachable: a caller composes ``load_allowlist`` then dispatch,
        and the load raising means dispatch is never invoked at all."""
        repo = make_bare_repo(tmp_path)
        missing_store = tmp_path / "missing-allowlist.json"

        with pytest.raises(AllowlistUnreadableError):
            allowed_origins = load_allowlist(missing_store)
            build_dispatch_payload(  # pragma: no cover - never reached
                project="p",
                snapshot="prd-p",
                origin_url="git@github.com:acme/widgets.git",
                repo_root=str(repo),
                pr_base="main",
                org="acme-org",
                allowed_origins=allowed_origins,
            )


class TestOperatorAddAndRemovePath:
    """Given the operator using the documented path
    (``scripts/manage_origin_allowlist.py``), the origin is added — and can
    be removed the same way."""

    def _run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(_MANAGE_CLI), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_operator_add_creates_the_store_and_registers_the_origin(self, tmp_path):
        store = tmp_path / "allowlist.json"
        origin = "git@github.com:acme/widgets.git"

        self._run_cli("--path", str(store), "add", origin)

        assert origin in load_allowlist(store)

    def test_operator_add_is_idempotent(self, tmp_path):
        store = tmp_path / "allowlist.json"
        origin = "git@github.com:acme/widgets.git"

        self._run_cli("--path", str(store), "add", origin)
        self._run_cli("--path", str(store), "add", origin)

        allowlist = load_allowlist(store)
        assert list(allowlist).count(origin) <= 1
        assert origin in allowlist

    def test_operator_remove_deregisters_the_origin(self, tmp_path):
        store = tmp_path / "allowlist.json"
        origin = "git@github.com:acme/widgets.git"
        self._run_cli("--path", str(store), "add", origin)
        assert origin in load_allowlist(store)

        self._run_cli("--path", str(store), "remove", origin)

        assert origin not in load_allowlist(store)

    def test_added_origin_then_clears_the_dispatch_allowlist_guard(self, tmp_path):
        """End-to-end: operator registers an origin via the documented path,
        and a dispatch for that origin now succeeds."""
        repo = make_bare_repo(tmp_path)
        store = tmp_path / "allowlist.json"
        origin = "git@github.com:acme/widgets.git"

        self._run_cli("--path", str(store), "add", origin)
        allowed_origins = load_allowlist(store)

        payload = build_dispatch_payload(
            project="p",
            snapshot="prd-p",
            origin_url=origin,
            repo_root=str(repo),
            pr_base="main",
            org="acme-org",
            allowed_origins=allowed_origins,
        )
        assert payload.origin_url == origin

        # An origin never registered is still refused.
        with pytest.raises(OriginNotAllowedError):
            build_dispatch_payload(
                project="p",
                snapshot="prd-p",
                origin_url="git@github.com:acme/rogue.git",
                repo_root=str(repo),
                pr_base="main",
                org="acme-org",
                allowed_origins=allowed_origins,
            )
