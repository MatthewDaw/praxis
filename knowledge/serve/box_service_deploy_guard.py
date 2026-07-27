"""Deploy-class command refusal for a build session (this ticket's acceptance floor).

R18's advisory lock exists to *serialize* commands that contend on a fixed host
port or a shared test fixture -- it was never meant to grant a deploy step safe
passage, only to keep two concurrent deploys (or a deploy racing a fixture-bound
suite) from clobbering each other. A deploy step invoked from inside a build
session is a different hazard class entirely: it is an outbound-publish action
with the same blast radius the push guard (R33) and the credential-reachability
boundary (R37) already exist to close off. So a deploy-class command is refused
outright -- never serialized, never run.

``guard_command`` is the single seam every command a build session runs must
pass through before ``box_service_host_lock.run_locked`` ever gets to classify
it as contending or not. It takes no allowlist, override, or force parameter --
there is deliberately no argument that could authorize a deploy command through
this function, so "no authorization path exists to permit it" is a property of
the call signature, not a runtime check that could be bypassed by passing a flag.
"""

from __future__ import annotations

#: Substrings identifying a deploy-class command, drawn from this repo's own
#: deploy surfaces (infra/package.json's ``npm run deploy`` -> ``cdk deploy
#: --require-approval never``; infra/README.md's ``npx cdk deploy``) plus the
#: general infra-as-code verbs a deploy-class command in any project session
#: would use to publish outside the box.
_DEPLOY_MARKERS = (
    "cdk deploy",
    "npm run deploy",
    "npx cdk deploy",
    "terraform apply",
    "aws cloudformation deploy",
    "aws cloudformation create-stack",
    "aws cloudformation update-stack",
    "serverless deploy",
    "sls deploy",
)


class DeployCommandRefused(RuntimeError):
    """Raised in place of running a deploy-class command. Names the refused
    command so the refusal is legible in logs/session output, and carries no
    field or mechanism by which the refusal could be waived."""

    def __init__(self, command: str) -> None:
        self.command = command
        super().__init__(
            f"deploy-class command refused: {command!r} — no authorization path "
            "exists to permit a deploy-class command from a build session"
        )


def is_deploy_command(command: str) -> bool:
    """True iff ``command`` is a deploy-class command -- the narrow class this
    guard exists to refuse outright, disjoint from
    ``box_service_host_lock.is_contending_command``'s fixture/port-bound class."""
    return any(marker in command for marker in _DEPLOY_MARKERS)


def guard_command(command: str) -> None:
    """Raise :class:`DeployCommandRefused` naming ``command`` iff it is a
    deploy-class command. No-op otherwise. Call this before any other
    command-execution seam (e.g. ``box_service_host_lock.run_locked``) ever
    sees the command, so a deploy-class command never reaches -- and is never
    classified by -- the advisory lock that wraps only fixture/port-bound
    commands."""
    if is_deploy_command(command):
        raise DeployCommandRefused(command)
