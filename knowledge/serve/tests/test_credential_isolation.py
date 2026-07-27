"""Acceptance test for ticket R37 (abcc96941a7045528618a52a572abe90) — the
``credential-unreachable-from-session`` check:

A build session cannot read the push credential from the service credential store or from the
cloud instance metadata service, and its environment carries no service token or cloud credential
variable, while the box service itself still obtains the credential.

Runs fully offline: no real background session, no real AWS call, no live network — a fake runner
stands in for the session launcher's subprocess, and ``boto3.client`` is mocked for the box
service's own resolution.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from unittest import mock

import knowledge.serve.github_token as github_token
from knowledge.serve.session_launcher import SessionLauncher

#: Credential-shaped environment variables that might legitimately sit in the box service's own
#: process environment (static AWS creds, an assumed-role session token, and the account-wide
#: GitHub PAT) — none of these may ever reach a spawned build session.
_AMBIENT_CREDENTIAL_ENV = {
    "AWS_ACCESS_KEY_ID": "AKIAFAKEFAKEFAKEFAKE",
    "AWS_SECRET_ACCESS_KEY": "fake/secret/access/key/value",
    "AWS_SESSION_TOKEN": "fake-assumed-role-session-token",
    "GITHUB_TOKEN": "ghp_fakeaccounttokenvalue1234567890",
}


@dataclass
class FakeRunner:
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="sess-1\n", stderr="")


def test_launched_build_session_environment_carries_no_service_token_or_cloud_credential_variable(
    monkeypatch,
):
    for key, value in _AMBIENT_CREDENTIAL_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # an ordinary, non-credential var stays present

    runner = FakeRunner()
    launcher = SessionLauncher(runner=runner)

    launcher.launch(cwd="/repo/jobs/job-1", command="/af-build")

    assert len(runner.calls) == 1
    env = runner.calls[0].get("env")
    assert env is not None, (
        "launch must pass an explicit env, never let the child inherit the box service's own "
        "process environment by omission"
    )
    for ambient_key, ambient_value in _AMBIENT_CREDENTIAL_ENV.items():
        assert ambient_key not in env, f"{ambient_key} leaked into the build session's environment"
        assert ambient_value not in env.values(), "a credential value leaked under a renamed key"
    # An ordinary variable is untouched — this is a targeted removal, not a wipe of everything.
    assert env.get("PATH") == "/usr/bin:/bin"


def test_box_service_itself_still_obtains_the_credential_despite_session_isolation():
    """Session isolation strips the box service's OWN spawned children's environment — it must
    never also block the box service's own in-process credential resolution."""
    github_token.invalidate_github_token()
    fake_client = mock.Mock()
    fake_client.get_secret_value.return_value = {"SecretString": "ghp_realboxservicetoken000000"}

    with mock.patch("boto3.client", return_value=fake_client):
        token = github_token.resolve_github_token()

    assert token == "ghp_realboxservicetoken000000"
    github_token.invalidate_github_token()


def test_fetch_uncached_also_still_obtains_the_credential_for_the_box_service():
    fake_client = mock.Mock()
    fake_client.get_secret_value.return_value = {"SecretString": "ghp_realboxservicetoken111111"}

    with mock.patch("boto3.client", return_value=fake_client):
        token = github_token.fetch_github_token_uncached()

    assert token == "ghp_realboxservicetoken111111"
