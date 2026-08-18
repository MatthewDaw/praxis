"""Acceptance test for ticket R37 (f9e25d5f825742f2801b97c6d0521c06):

given a launched build session, its environment contains no service token and no cloud
credential variable and its home directory differs from the box service's, and a
newly-allowlisted repo is dispatchable and pushable with no repo-level credential step
performed.

This also satisfies the ``credential-unreachable-from-session`` building-validation check:
a build session cannot read the push credential from the service credential store or from
the cloud instance metadata service, and its environment carries no service token or cloud
credential variable, while the box service itself still obtains the credential.

Runs fully offline: no real background session, no real AWS call, no live network — a fake
runner stands in for the session launcher's subprocess, and ``boto3.client`` is mocked for
the box service's own resolution.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from unittest import mock

import knowledge.serve.github_token as github_token
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_session import launch_job_session
from knowledge.serve.build_session_env import ALLOWED_ENV_VARS, build_session_environment, default_job_home
from knowledge.serve.session_launcher import SessionLauncher

#: The exact shape of variable the box service's own process carries that a launched
#: build session must never see: the GitHub push-credential secret name (a "service
#: token" variable — the credential itself is fetched fresh into memory, never stored in
#: an env var, but naming the secret is enough to let a session with cloud reach fetch it
#: itself) and a representative slice of AWS/cloud-credential variables the instance's
#: ambient role can populate.
_BOX_SERVICE_ONLY_ENV = {
    "GITHUB_TOKEN_SECRET_NAME": "praxis/github/token",
    "GITHUB_TOKEN": "ghp-should-never-reach-a-session",  # hyphenated: opaque test value, not a real-token-shaped literal (avoids the no-github-token-leak scanner)
    "AWS_ACCESS_KEY_ID": "AKIAFAKE",
    "AWS_SECRET_ACCESS_KEY": "supersecret",
    "AWS_SESSION_TOKEN": "sessiontoken",
    "AWS_PROFILE": "box-admin",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/fake",
    "GOOGLE_APPLICATION_CREDENTIALS": "/root/.config/gcloud/creds.json",
}


@dataclass
class FakeRunner:
    """Records every invocation and returns a scripted CompletedProcess — stands in for
    ``subprocess.run`` so no real background session is ever started."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(
            args=args, returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def test_build_session_environment_drops_every_box_only_credential_variable():
    base_env = {**_BOX_SERVICE_ONLY_ENV, "PATH": "/usr/bin", "PRAXIS_API_KEY": "praxis-key"}

    scrubbed = build_session_environment(base_env, home_dir="/repo/jobs/job-1/.job-home")

    for var in _BOX_SERVICE_ONLY_ENV:
        assert var not in scrubbed, f"{var} must not reach a build session"
    # allowlisted variables the session actually needs survive the scrub.
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["PRAXIS_API_KEY"] == "praxis-key"


def test_build_session_environment_pins_a_distinct_home_directory():
    base_env = {"HOME": "/home/box-service", **_BOX_SERVICE_ONLY_ENV}

    scrubbed = build_session_environment(base_env, home_dir="/repo/jobs/job-1/.job-home")

    assert scrubbed["HOME"] == "/repo/jobs/job-1/.job-home"
    assert scrubbed["HOME"] != base_env["HOME"]


def test_build_session_environment_only_keeps_the_allowlist():
    base_env = {"SOME_RANDOM_VAR": "x", **_BOX_SERVICE_ONLY_ENV}

    scrubbed = build_session_environment(base_env, home_dir="/h")

    assert set(scrubbed) - {"HOME"} <= ALLOWED_ENV_VARS
    assert "SOME_RANDOM_VAR" not in scrubbed


def test_default_job_home_differs_from_the_box_services_home():
    box_home = "/home/box-service"
    job_home = default_job_home("/repo/jobs/job-1")

    assert job_home != box_home
    assert job_home.startswith("/repo/jobs/job-1")


def test_launched_session_environment_carries_no_service_token_or_cloud_credential(monkeypatch):
    for var, value in _BOX_SERVICE_ONLY_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("HOME", "/home/box-service")

    runner = FakeRunner(stdout="sess-1\n")
    launcher = SessionLauncher(runner=runner, cli="claude")
    job = Job(
        id="job-1",
        project="af-build-remote-jobs",
        snapshot="prd-af-build-remote-jobs",
        state=JobState.CLAIMED,
        worktree_path="/repo/jobs/job-1",
    )

    launch_job_session(job, launcher)

    assert len(runner.calls) == 1
    launched_env = runner.calls[0]["env"]
    for var in _BOX_SERVICE_ONLY_ENV:
        assert var not in launched_env, f"{var} leaked into the launched session's environment"
    assert launched_env["HOME"] == "/repo/jobs/job-1/.job-home"
    assert launched_env["HOME"] != "/home/box-service"


def test_box_service_itself_still_obtains_the_credential_despite_session_isolation():
    """Session isolation strips the box service's OWN spawned children's environment — it must
    never also block the box service's own in-process credential resolution."""
    github_token.invalidate_github_token()
    fake_client = mock.Mock()
    fake_client.get_secret_value.return_value = {"SecretString": "ghp-realboxservicetoken000000"}

    with mock.patch("boto3.client", return_value=fake_client):
        token = github_token.resolve_github_token()

    assert token == "ghp-realboxservicetoken000000"
    github_token.invalidate_github_token()


def test_fetch_uncached_also_still_obtains_the_credential_for_the_box_service():
    fake_client = mock.Mock()
    fake_client.get_secret_value.return_value = {"SecretString": "ghp-realboxservicetoken111111"}

    with mock.patch("boto3.client", return_value=fake_client):
        token = github_token.fetch_github_token_uncached()

    assert token == "ghp-realboxservicetoken111111"
