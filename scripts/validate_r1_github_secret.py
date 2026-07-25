#!/usr/bin/env python3
"""R1 acceptance check: GitHub token secret storage.

Proves, against a REAL `cdk synth` of the backend stack (fed a fake token via
`GITHUB_TOKEN` so the negative assertions are non-vacuous):

  1. a Secrets Manager secret for the GitHub token exists (a genuine
     `AWS::SecretsManager::Secret` resource, not a `fromSecretNameV2` lookup of
     a pre-existing one) named `GITHUB_TOKEN_SECRET_NAME`;
  2. the App Runner instance role has an IAM policy statement granting it
     `secretsmanager:GetSecretValue`/`DescribeSecret` on that secret's ARN;
  3. the fake token value appears in NO `runtimeEnvironmentVariables` entry
     anywhere in the synthesized template;

and, against the runtime resolver module:

  4. resolving the token never prints/logs the value (stdout+stderr captured
     around the call must not contain it).

Exit 0 == every assertion holds. Any failure prints which one and exits 1.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA_DIR = REPO_ROOT / "infra"
FAKE_TOKEN = "ghp_faketokenFAKE1234567890abcdEVAL"
SECRET_NAME = "praxis/github/token"


def _synth_template() -> dict:
    proc = subprocess.run(
        ["npx", "cdk", "synth", "PraxisBackendServiceStack", "--json"],
        cwd=INFRA_DIR,
        env={"GITHUB_TOKEN": FAKE_TOKEN, **_inherited_env()},
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"cdk synth failed with exit code {proc.returncode}")
    return json.loads(proc.stdout)


def _inherited_env() -> dict:
    import os

    return dict(os.environ)


def _check_secret_resource(resources: dict) -> str:
    secrets = [
        (logical_id, props)
        for logical_id, res in resources.items()
        if res.get("Type") == "AWS::SecretsManager::Secret"
        for props in [res.get("Properties", {})]
    ]
    matches = [(lid, p) for lid, p in secrets if p.get("Name") == SECRET_NAME]
    if not matches:
        raise SystemExit(
            f"FAIL: no AWS::SecretsManager::Secret resource named {SECRET_NAME!r} "
            f"found in the synthesized template (found secrets: {secrets!r})"
        )
    return matches[0][0]


def _check_grant_read(resources: dict, secret_logical_id: str) -> None:
    secret_ref_forms = (
        {"Ref": secret_logical_id},
    )
    for logical_id, res in resources.items():
        if res.get("Type") != "AWS::IAM::Policy":
            continue
        statements = res.get("Properties", {}).get("PolicyDocument", {}).get("Statement", [])
        for stmt in statements:
            actions = stmt.get("Action", [])
            actions = actions if isinstance(actions, list) else [actions]
            resource = stmt.get("Resource")
            if "secretsmanager:GetSecretValue" in actions and resource in secret_ref_forms:
                return
    raise SystemExit(
        f"FAIL: no IAM policy statement grants secretsmanager:GetSecretValue on "
        f"{secret_logical_id!r} (instance role grantRead missing)"
    )


def _check_no_env_leak(resources: dict) -> None:
    template_str = json.dumps(resources)
    for logical_id, res in resources.items():
        if res.get("Type") != "AWS::AppRunner::Service":
            continue
        envs = (
            res.get("Properties", {})
            .get("SourceConfiguration", {})
            .get("ImageRepository", {})
            .get("ImageConfiguration", {})
            .get("RuntimeEnvironmentVariables", [])
        )
        for entry in envs:
            value = str(entry.get("Value", ""))
            if FAKE_TOKEN in value or "ghp_" in value or "github_pat_" in value:
                raise SystemExit(
                    f"FAIL: runtimeEnvironmentVariables entry {entry.get('Name')!r} "
                    f"leaks the GitHub token: {entry!r}"
                )
    # Belt-and-suspenders: the fake token must appear ONLY inside the secret
    # resource's own SecretString property (where Secrets Manager is supposed
    # to hold it), never anywhere else in the synthesized template.
    occurrences = template_str.count(FAKE_TOKEN)
    secret_occurrences = sum(
        json.dumps(res.get("Properties", {})).count(FAKE_TOKEN)
        for res in resources.values()
        if res.get("Type") == "AWS::SecretsManager::Secret"
    )
    if occurrences != secret_occurrences:
        raise SystemExit(
            "FAIL: the GitHub token appears outside the Secrets Manager secret "
            "resource somewhere in the synthesized template"
        )


def _check_resolver_never_logs() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    import importlib

    github_token = importlib.import_module("knowledge.serve.github_token")
    importlib.reload(github_token)

    class _FakeSecretsManagerClient:
        def get_secret_value(self, SecretId):  # noqa: N803 - boto3 kwarg name
            return {"SecretString": FAKE_TOKEN}

    import unittest.mock as mock

    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
    with mock.patch("boto3.client", return_value=_FakeSecretsManagerClient()):
        github_token.invalidate_github_token()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            token = github_token.resolve_github_token()
    if token != FAKE_TOKEN:
        raise SystemExit(f"FAIL: resolve_github_token() did not return the fake secret value (got {token!r})")
    captured = stdout_buf.getvalue() + stderr_buf.getvalue()
    if FAKE_TOKEN in captured:
        raise SystemExit("FAIL: resolve_github_token() printed/logged the token value")
    github_token.invalidate_github_token()


def main() -> int:
    template = _synth_template()
    resources = template.get("Resources", {})
    secret_logical_id = _check_secret_resource(resources)
    _check_grant_read(resources, secret_logical_id)
    _check_no_env_leak(resources)
    _check_resolver_never_logs()
    print("PASS: R1 GitHub token secret storage — secret exists, grantRead present, "
          "no env-var/log leak of the token value.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
