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
TOKEN_PATTERN = re.compile(r"(github_pat_|ghp_)[A-Za-z0-9_]{10,}")


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

    # 4) Defense-in-depth: no literal GitHub token ever landed in the synthesized
    #    template text itself.
    if TOKEN_PATTERN.search(template_path.read_text()):
        _fail("a GitHub token-shaped literal appears in the synthesized template")

    print("OK: GitHub token secret + instance-role grantRead + no plaintext env var")


if __name__ == "__main__":
    main()
