#!/usr/bin/env python3
"""R1 acceptance check: verify the synthesized backend CDK template stores the
GitHub token in a NEW Secrets Manager secret with read access granted to the
App Runner instance role, and never as a plaintext runtimeEnvironmentVariables
entry.

Runs `cdk synth PraxisBackendServiceStack` (via the repo's own infra/
package.json) and inspects the resulting CloudFormation template. Exits
non-zero (with a message on stderr) on any violation.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA_DIR = REPO_ROOT / "infra"
STACK_NAME = "PraxisBackendServiceStack"
TOKEN_PATTERN = re.compile(r"(github_pat_|ghp_|gho_)[A-Za-z0-9_]{10,}")

# Env-var names that imply the VALUE is a credential rather than a pointer/identifier.
# Deliberately excludes Cognito pool/client ids, which are public identifiers, not secrets.
_SECRET_NAME_PATTERN = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)")


def _is_cfn_reference(value: object) -> bool:
    """True when ``value`` is a CloudFormation intrinsic (``Ref``/``Fn::*``) rather than a
    literal string. A reference resolves to an ARN or an identifier CloudFormation supplies
    at deploy time, so it never embeds a credential in the template or the service config."""
    return isinstance(value, dict)


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    subprocess.run(["npx", "cdk", "synth", STACK_NAME], cwd=INFRA_DIR, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    template_path = INFRA_DIR / "cdk.out" / f"{STACK_NAME}.template.json"
    if not template_path.exists():
        _fail(f"expected synth output at {template_path}")
    template = json.loads(template_path.read_text())
    resources = template.get("Resources", {})

    # 1) A Secrets Manager secret for the GitHub token exists.
    github_secrets = [
        (rid, r) for rid, r in resources.items()
        if r.get("Type") == "AWS::SecretsManager::Secret" and "github" in rid.lower()
    ]
    if not github_secrets:
        _fail("no AWS::SecretsManager::Secret resource for the GitHub token found")
    secret_id, _ = github_secrets[0]

    # 2) The instance role (attached to the App Runner service) has grantRead on it:
    #    an IAM::Policy resource with secretsmanager:GetSecretValue whose statement
    #    resource references this secret, attached to a role tied to the service.
    instance_roles = [rid for rid, r in resources.items() if r.get("Type") == "AWS::IAM::Role"
                       and "InstanceRole" in rid]
    if not instance_roles:
        _fail("no InstanceRole found in the synthesized template")
    instance_role_id = instance_roles[0]

    def _references(obj, target: str) -> bool:
        return target in json.dumps(obj)

    granting_policies = [
        r for r in resources.values()
        if r.get("Type") == "AWS::IAM::Policy"
        and _references(r.get("Properties", {}).get("Roles"), instance_role_id)
        and "secretsmanager:GetSecretValue" in json.dumps(r.get("Properties", {}).get("PolicyDocument", {}))
        and _references(r.get("Properties", {}).get("PolicyDocument"), secret_id)
    ]
    if not granting_policies:
        _fail(f"no IAM policy grants secretsmanager:GetSecretValue on {secret_id} to {instance_role_id}")

    # 3) The App Runner service never carries the token in a plaintext env var:
    #    no runtimeEnvironmentVariables entry is literally the token, and no
    #    entry's name suggests a raw secret value was passed as plaintext.
    services = [r for r in resources.values() if r.get("Type") == "AWS::AppRunner::Service"]
    if not services:
        _fail("no AWS::AppRunner::Service resource found")
    for svc in services:
        env_vars = (
            svc.get("Properties", {})
            .get("SourceConfiguration", {})
            .get("ImageRepository", {})
            .get("ImageConfiguration", {})
            .get("RuntimeEnvironmentVariables", [])
        )
        for entry in env_vars:
            name = str(entry.get("Name", ""))
            value = json.dumps(entry.get("Value", ""))
            if name.upper() in {"GITHUB_TOKEN", "GITHUB_PAT"}:
                _fail(f"runtimeEnvironmentVariables carries a plaintext GitHub token entry: {name}")
            if TOKEN_PATTERN.search(value):
                _fail(f"runtimeEnvironmentVariables entry {name!r} looks like a raw GitHub token")

            # Generalized guard (2026-07-28): `OPENROUTER_API_KEY` sat here as a plaintext
            # literal for months, echoed in full by `apprunner describe-service` and the
            # console to anyone holding `apprunner:DescribeService` -- the exact leak the
            # GitHub-token rule above prevents, in the same list, just under a name the
            # GitHub-specific check never looked at. Secret-NAMED entries are only allowed
            # when they carry a CloudFormation reference (an ARN/Ref that App Runner
            # resolves privately) rather than a literal value; a real secret value belongs
            # in `RuntimeEnvironmentSecrets`, never here.
            if _SECRET_NAME_PATTERN.search(name) and not _is_cfn_reference(entry.get("Value")):
                if not name.upper().endswith(("_SECRET_NAME", "_SECRET_ARN")):
                    _fail(
                        f"runtimeEnvironmentVariables entry {name!r} has a secret-suggesting "
                        "name with a literal value -- App Runner echoes these in plaintext via "
                        "describe-service/console. Move it to runtimeEnvironmentSecrets (an ARN "
                        "reference), or, if it is genuinely only a secret's NAME, suffix it "
                        "_SECRET_NAME."
                    )

    # 4) Defense-in-depth: no literal GitHub token ever landed in the synthesized
    #    template text itself.
    if TOKEN_PATTERN.search(template_path.read_text()):
        _fail("a GitHub token-shaped literal appears in the synthesized template")

    print(
        "OK: GitHub token secret + instance-role grantRead + no plaintext secret env vars"
    )


if __name__ == "__main__":
    main()
