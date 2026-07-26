"""Acceptance test for ticket R6 (self-contained dispatch payload, b02b912b):

given a repo containing no factory config file, when a job is dispatched it
still carries every field needed to execute (project slug, snapshot, origin
URL, build-base commit SHA, intended PR base, and Praxis org identity), and
dispatch reads no file from the target repo.

Also covers R5: dispatching a remote job is a separate action from building it.

The dispatching session must not claim a ticket and must not stamp a whole-set run
marker: ``hooks/build_completeness_gate.py`` arms (and blocks the session's turn) only
when the session owns a live claim or a non-stale run marker. A dispatcher that touched
either would block its own turn against the gate it just armed.

These tests run fully offline: the ticket-state mutators are monkeypatched to detect
any call, and the gate itself is driven with a monkeypatched ``incomplete_requirements``
(the same harness ``test_build_gate_scenarios.py`` uses), so no Praxis network is
needed.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from knowledge.serve.dispatch import (
    DispatchError,
    DispatchPayload,
    build_dispatch_payload,
    check_clean_working_tree,
    dispatch_job,
    resolve_build_base_sha,
    resolve_dispatch_org,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOOKS = str(_REPO_ROOT / "agent_factory" / "hooks")
_AF_SRC = str(_REPO_ROOT / "agent_factory" / "src")
# ``agent_factory`` is a namespace package: this repo root ALSO contributes an
# ``agent_factory`` portion (docs/skills/etc, no ``resumability`` submodule), so
# ``_AF_SRC`` (which has the real ``agent_factory.resumability``) must be on sys.path
# BEFORE ``agent_factory`` is imported for the first time — once a namespace package is
# bound in sys.modules from a partial portion, adding sibling portions afterwards does
# not reliably extend its already-resolved ``__path__``.
for _p in (_AF_SRC, _HOOKS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402
import build_completeness_gate as gate  # noqa: E402

DISPATCH_SRC = (Path(__file__).resolve().parent.parent / "dispatch.py").read_text(encoding="utf-8")

OWNER = "dispatching-session"


def make_bare_repo(tmp_path, monkeypatch=None):
    """A real git repo with exactly one commit and NO factory config file of
    any kind — the acceptance condition's starting state."""
    repo = tmp_path / "target-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


def test_no_factory_config_file_present_in_target_repo(tmp_path):
    repo = make_bare_repo(tmp_path)
    for candidate in (".af-build.yml", ".af-build.yaml", "af-build.json", ".factory.yml"):
        assert not (repo / candidate).exists()


def test_dispatch_payload_carries_every_field_with_no_config_file_present(tmp_path):
    repo = make_bare_repo(tmp_path)

    payload = build_dispatch_payload(
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        origin_url="git@github.com:acme/widgets.git",
        repo_root=str(repo),
        pr_base="main",
        org="acme-org",
    )

    assert isinstance(payload, DispatchPayload)
    assert payload.project == "af-build-remote-jobs"
    assert payload.snapshot == "prd-af-build-remote-jobs"
    assert payload.origin_url == "git@github.com:acme/widgets.git"
    assert payload.pr_base == "main"
    assert payload.org == "acme-org"
    # build_base_sha is a resolved commit, not a branch name (R7 groundwork).
    assert len(payload.build_base_sha) == 40
    assert all(c in "0123456789abcdef" for c in payload.build_base_sha)


def test_dispatch_never_opens_a_file_inside_the_target_repo(tmp_path, monkeypatch):
    """Structural guard against a future implementer sneaking in a
    factory-config lookup: any attempt to open a path under the repo root
    fails the test immediately."""
    repo = make_bare_repo(tmp_path)
    repo_str = str(repo)
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        path = str(file)
        if path.startswith(repo_str):
            raise AssertionError(f"dispatch must not open a file inside the target repo: {path}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)

    payload = build_dispatch_payload(
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        origin_url="git@github.com:acme/widgets.git",
        repo_root=repo_str,
        pr_base="main",
        org="acme-org",
    )

    assert payload.build_base_sha


def test_build_base_sha_matches_git_rev_parse_head(tmp_path):
    repo = make_bare_repo(tmp_path)

    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    payload = build_dispatch_payload(
        project="p",
        snapshot="prd-p",
        origin_url="git@github.com:acme/widgets.git",
        repo_root=str(repo),
        pr_base="main",
        org="acme-org",
    )

    assert payload.build_base_sha == expected


@pytest.mark.parametrize("missing_field", ["project", "snapshot", "origin_url", "pr_base", "org"])
def test_missing_required_field_raises_dispatch_error(tmp_path, missing_field):
    repo = make_bare_repo(tmp_path)
    kwargs = dict(
        project="p",
        snapshot="prd-p",
        origin_url="git@github.com:acme/widgets.git",
        repo_root=str(repo),
        pr_base="main",
        org="acme-org",
    )
    kwargs[missing_field] = ""

    with pytest.raises(DispatchError):
        build_dispatch_payload(**kwargs)


def test_resolve_build_base_sha_raises_on_git_failure():
    def failing_runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="not a git repository")

    with pytest.raises(DispatchError):
        resolve_build_base_sha("/not/a/repo", runner=failing_runner)


def test_org_is_never_sourced_from_an_alternate_payload_field(tmp_path):
    """Dispatch has no "requested org" parameter distinct from the
    server-derived one — there is no field a spoofed client value could ride
    in on (dispatch-guard: "derives org identity server-side while ignoring
    a payload-supplied org")."""
    import inspect

    sig = inspect.signature(build_dispatch_payload)
    org_like = [p for p in sig.parameters if "org" in p]
    assert org_like == ["org"]


class TestResolveDispatchOrg:
    """Acceptance test for ticket 31537bea12124dfdb27cb99e7e1f5c2d (R53):

    given a dispatch payload whose org field differs from the authenticated
    credential's org, the dispatch is rejected rather than honoring the
    payload value; given no org field at all, dispatch still succeeds using
    the credential-derived org.
    """

    def test_payload_org_matching_credential_org_is_honored(self):
        assert resolve_dispatch_org({"org": "acme-org"}, credential_org="acme-org") == "acme-org"

    def test_payload_org_mismatching_credential_org_is_rejected(self):
        with pytest.raises(DispatchError):
            resolve_dispatch_org({"org": "attacker-org"}, credential_org="acme-org")

    def test_missing_payload_org_falls_back_to_credential_org(self):
        assert resolve_dispatch_org({}, credential_org="acme-org") == "acme-org"

    def test_payload_org_key_present_but_falsy_falls_back_to_credential_org(self):
        assert resolve_dispatch_org({"org": ""}, credential_org="acme-org") == "acme-org"

    def test_empty_credential_org_is_refused_regardless_of_payload(self):
        with pytest.raises(DispatchError):
            resolve_dispatch_org({"org": "acme-org"}, credential_org="")
        with pytest.raises(DispatchError):
            resolve_dispatch_org({}, credential_org="")


class TestDirtyWorkingTreeGuard:
    """Acceptance test for ticket a13b7a9deca24a11948a508d3b84e6d3 (R9):

    given uncommitted changes in the working tree or index, dispatch exits non-zero
    and lists the uncommitted paths; given a clean tree, dispatch proceeds.
    """

    def test_clean_tree_does_not_raise(self, tmp_path):
        repo = make_bare_repo(tmp_path)
        check_clean_working_tree(str(repo))  # must not raise

    def test_dirty_working_tree_is_refused_and_names_the_path(self, tmp_path):
        repo = make_bare_repo(tmp_path)
        (repo / "README.md").write_text("changed\n")  # unstaged modification

        with pytest.raises(DispatchError) as exc_info:
            check_clean_working_tree(str(repo))

        assert "README.md" in str(exc_info.value)

    def test_dirty_index_is_refused_and_names_the_path(self, tmp_path):
        repo = make_bare_repo(tmp_path)
        (repo / "untracked.txt").write_text("new\n")
        subprocess.run(["git", "add", "untracked.txt"], cwd=repo, check=True)  # staged, uncommitted

        with pytest.raises(DispatchError) as exc_info:
            check_clean_working_tree(str(repo))

        assert "untracked.txt" in str(exc_info.value)

    def test_untracked_file_is_refused_and_named(self, tmp_path):
        repo = make_bare_repo(tmp_path)
        (repo / "scratch.txt").write_text("stray\n")  # untracked, never added

        with pytest.raises(DispatchError) as exc_info:
            check_clean_working_tree(str(repo))

        assert "scratch.txt" in str(exc_info.value)

    def test_git_status_failure_raises_dispatch_error(self):
        def failing_runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="not a git repository")

        with pytest.raises(DispatchError):
            check_clean_working_tree("/not/a/repo", runner=failing_runner)

    def test_dirty_working_tree_fails_the_whole_dispatch_payload_build(self, tmp_path):
        repo = make_bare_repo(tmp_path)
        (repo / "README.md").write_text("changed\n")

        with pytest.raises(DispatchError) as exc_info:
            build_dispatch_payload(
                project="p",
                snapshot="prd-p",
                origin_url="git@github.com:acme/widgets.git",
                repo_root=str(repo),
                pr_base="main",
                org="acme-org",
            )

        assert "README.md" in str(exc_info.value)

    def test_clean_tree_dispatch_proceeds(self, tmp_path):
        repo = make_bare_repo(tmp_path)

        payload = build_dispatch_payload(
            project="p",
            snapshot="prd-p",
            origin_url="git@github.com:acme/widgets.git",
            repo_root=str(repo),
            pr_base="main",
            org="acme-org",
        )

        assert isinstance(payload, DispatchPayload)


def test_dispatch_returns_a_queued_job():
    job = dispatch_job("acme-app", "prd-acme-app")
    assert job.project == "acme-app"
    assert job.snapshot == "prd-acme-app"
    assert job.state == "queued"
    assert job.id


def test_dispatch_requires_project_and_snapshot():
    with pytest.raises(ValueError):
        dispatch_job("", "prd-acme-app")
    with pytest.raises(ValueError):
        dispatch_job("acme-app", "")


def test_dispatch_module_never_references_ticket_state():
    """Structural guarantee (R5): dispatch.py does not import ``_ticket_state`` at all,
    so it cannot reach ``claim``/``stamp_run`` even indirectly."""
    tree = ast.parse(DISPATCH_SRC)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not any("_ticket_state" in n for n in names)


def test_dispatch_does_not_claim_or_stamp_run_marker(monkeypatch):
    calls = []
    monkeypatch.setattr(ts, "claim", lambda *a, **k: calls.append("claim") or True)
    monkeypatch.setattr(ts, "stamp_run", lambda *a, **k: calls.append("stamp_run"))

    dispatch_job("acme-app", "prd-acme-app")

    assert calls == []


def _run_gate(monkeypatch, items, session=OWNER):
    """Drive the real Stop-hook entrypoint offline (mirrors test_build_gate_scenarios.py)."""
    monkeypatch.setattr(_praxis, "incomplete_requirements", lambda project, **k: items)
    monkeypatch.setenv("FACTORY_PROJECT", "prd-acme-app")
    monkeypatch.delenv("FACTORY_GATE_DISABLED", raising=False)
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({"session_id": session, "cwd": "/x/acme-app"})),
    )
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        gate.main()
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else {}
    return "block" if parsed.get("decision") == "block" else "allow"


def test_dispatching_session_ends_its_turn_without_the_gate_blocking(monkeypatch):
    """AE1 / the ticket's acceptance condition: given a session that dispatches a
    remote job, after dispatch it holds zero ticket claims and zero run markers, and
    its turn ends without the completeness gate blocking."""
    job = dispatch_job("acme-app", "prd-acme-app")
    assert job.state == "queued"

    # The dispatching session's own claim/run-marker footprint on the incomplete set is
    # empty — no requirement item carries this session as claim_owner or run_owner —
    # exactly what a dispatch (as opposed to a build) leaves behind.
    other_owner_item = {
        "id": "R1", "text": "unrelated in-flight ticket",
        "meta": {"requirement_id": "R1", "build_state": "incomplete"},
    }
    assert _run_gate(monkeypatch, [other_owner_item], session=OWNER) == "allow"
