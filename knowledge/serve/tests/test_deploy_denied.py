"""Proves the acceptance condition directly: a deploy-class command invoked
from a remote job's build session is refused with the command named, no
authorization path exists to permit it, and the host advisory lock
(``box_service_host_lock``) wraps only fixture/fixed-host-port commands --
never a deploy step."""

from __future__ import annotations

import inspect

import pytest

from knowledge.serve.box_service_deploy_guard import (
    DeployCommandRefused,
    guard_command,
    is_deploy_command,
)
from knowledge.serve.box_service_host_lock import is_contending_command, run_locked


def test_classifies_deploy_class_commands_from_this_repos_own_deploy_surfaces():
    assert is_deploy_command("cdk deploy --require-approval never")
    assert is_deploy_command("npm run deploy")
    assert is_deploy_command("npx cdk deploy PraxisDevBoxStack")
    assert is_deploy_command("terraform apply -auto-approve")
    assert not is_deploy_command("uv run --group dev pytest knowledge/serve/tests -q")
    assert not is_deploy_command("docker compose up -d --wait db")


def test_guard_command_refuses_a_deploy_class_command_naming_it():
    with pytest.raises(DeployCommandRefused) as exc_info:
        guard_command("npx cdk deploy PraxisFrontendSiteStack")

    assert "npx cdk deploy PraxisFrontendSiteStack" in str(exc_info.value)


def test_guard_command_is_a_noop_for_a_non_deploy_command():
    guard_command("uv run --group dev pytest knowledge/serve/tests -q")  # does not raise


def test_run_locked_refuses_a_deploy_command_before_ever_invoking_the_runner():
    calls: list[str] = []

    def runner():
        calls.append("ran")
        return 0

    with pytest.raises(DeployCommandRefused) as exc_info:
        run_locked("repo-a", "cdk deploy --require-approval never", runner)

    assert "cdk deploy --require-approval never" in str(exc_info.value)
    assert calls == []  # the runner is never reached -- refused, not merely serialized


def test_guard_command_signature_has_no_authorization_or_override_parameter():
    # "No authorization path exists to permit it" is a property of the call
    # signature itself: there is no flag this function accepts that could
    # wave a deploy-class command through.
    params = set(inspect.signature(guard_command).parameters)
    assert params == {"command"}


def test_advisory_lock_never_classifies_a_deploy_command_as_contending():
    # The lock's scope stays limited to fixture/fixed-host-port commands --
    # a deploy-class command is refused upstream (in run_locked/guard_command)
    # and must never be something the lock itself would serialize instead.
    assert not is_contending_command("cdk deploy --require-approval never")
    assert not is_contending_command("npm run deploy")
