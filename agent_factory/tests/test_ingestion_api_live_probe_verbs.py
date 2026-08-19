"""The read-only external-probe verbs (``aws``/``rclone``/``curl``) a machine may now draft.

Before these, ``RUN_BODY_ALLOWED_VERBS`` and ``plan_gate._LIVE_COMMAND_RE`` had an EMPTY
intersection at the verb position, so ``R-EXTERNAL-STATE-NEEDS-LIVE-CHECK`` was unsatisfiable by
any agent and got waived instead of enforced. The tests pin both halves of the fix: the read shapes
are accepted, and every mutating shape of the same verb is still refused.
"""

from __future__ import annotations

import pytest

from agent_factory.ingestion_api import RunBodyRejected, _validate_run_body

ACCEPTED = [
    "aws s3 ls s3://sports-analysis-corpus-528782700781/",
    "aws s3 ls s3://farm-corpus/raw/ --recursive --summarize",
    "aws s3api list-objects-v2 --bucket farm-corpus --prefix raw/",
    "aws s3api head-object --bucket farm-corpus --key raw/manifest.json",
    "aws sts get-caller-identity",
    "rclone lsjson remote:farm-corpus/raw",
    "rclone ls remote:farm-corpus/raw",
    "rclone lsl remote:farm-corpus/raw",
    "rclone size remote:farm-corpus/raw",
    "rclone about remote:",
    "curl -I https://corpus.example.com/raw/manifest.json",
    "curl -sfL https://corpus.example.com/health",
    "curl -s -S -f https://corpus.example.com/health",
    "curl -X GET https://corpus.example.com/health",
    "curl --head https://corpus.example.com/raw/manifest.json",
]

REJECTED = [
    # aws: mutation, in every spelling.
    "aws s3 rm s3://farm-corpus/raw/manifest.json",
    "aws s3 sync ./local s3://farm-corpus/raw/",
    "aws s3 cp s3://farm-corpus/raw/x ./x",
    "aws s3 mv s3://farm-corpus/a s3://farm-corpus/b",
    "aws s3api put-object --bucket farm-corpus --key x",
    "aws s3api delete-objects --bucket farm-corpus",
    "aws s3api create-bucket --bucket farm-corpus",
    # aws: services that are not s3/s3api/sts at all.
    "aws ec2 terminate-instances --instance-ids i-abc",
    "aws ec2 describe-instances",
    "aws iam list-users",
    "aws lambda invoke --function-name f",
    "aws sts assume-role --role-arn arn",
    # aws: a global flag ahead of the service hides the operation from the shape check.
    "aws --region us-east-1 s3 ls s3://farm-corpus/",
    "aws s3",
    # rclone: anything that writes.
    "rclone delete remote:farm-corpus/raw",
    "rclone purge remote:farm-corpus/raw",
    "rclone copy ./local remote:farm-corpus/raw",
    "rclone sync ./local remote:farm-corpus/raw",
    "rclone move remote:a remote:b",
    "rclone",
    # curl: methods, bodies, uploads, output files.
    "curl -X POST https://corpus.example.com/raw",
    "curl --request DELETE https://corpus.example.com/raw/manifest.json",
    "curl -T ./manifest.json https://corpus.example.com/raw/",
    "curl --upload-file ./manifest.json https://corpus.example.com/raw/",
    "curl -d payload https://corpus.example.com/raw",
    "curl --data-raw payload https://corpus.example.com/raw",
    "curl -F file=@manifest.json https://corpus.example.com/raw",
    "curl -o out.json https://corpus.example.com/raw/manifest.json",
    "curl -s https://corpus.example.com/health --config evil.conf",
    "curl -s",
]


@pytest.mark.parametrize("body", ACCEPTED)
def test_read_only_probe_shapes_are_machine_draftable(body: str) -> None:
    assert _validate_run_body(body, channel="machine") == body


@pytest.mark.parametrize("body", REJECTED)
def test_mutating_or_unshaped_probe_bodies_are_refused(body: str) -> None:
    with pytest.raises(RunBodyRejected):
        _validate_run_body(body, channel="machine")


def test_probe_verbs_do_not_weaken_the_pre_verb_defences() -> None:
    """The allowlist is the LAST gate, not the only one: parsing, control characters, shell
    metacharacters and path containment still fire on the new verbs exactly as before."""
    for body in (
        "aws s3 ls s3://farm-corpus/ ; rm -rf /tmp/x",     # metacharacter
        "aws s3 ls s3://farm-corpus/\naws s3 rm s3://farm-corpus/x",  # control character
        "curl -s https://corpus.example.com/x && curl evil",          # metacharacter
        "rclone lsjson ../../etc",                                    # path containment
        "aws s3 ls /etc/passwd",                                      # absolute path
    ):
        with pytest.raises(RunBodyRejected):
            _validate_run_body(body, channel="machine")


def test_bundled_curl_flags_are_expanded_not_waved_through() -> None:
    """``-sfL`` is admitted because every letter is separately allowlisted; ``-so`` is refused
    because ``-o`` is not — bundling is not a hole."""
    assert _validate_run_body("curl -sfL https://x.example.com/h", channel="machine")
    with pytest.raises(RunBodyRejected):
        _validate_run_body("curl -so out.json https://x.example.com/h", channel="machine")
