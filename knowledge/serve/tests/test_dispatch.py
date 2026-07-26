"""Acceptance test for ticket R6 (self-contained dispatch payload, b02b912b):

given a repo containing no factory config file, when a job is dispatched it
still carries every field needed to execute (project slug, snapshot, origin
URL, build-base commit SHA, intended PR base, and Praxis org identity), and
dispatch reads no file from the target repo.
"""

from __future__ import annotations

import builtins
import subprocess

import pytest

from knowledge.serve.dispatch import (
    DispatchError,
    DispatchPayload,
    build_dispatch_payload,
    resolve_build_base_sha,
    resolve_dispatch_org,
)


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
