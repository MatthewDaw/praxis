"""R27 acceptance: af-clean's risk-stratified commit stack (B25/B45/B26) and its unwind.

Acceptance (verbatim from the ticket): truncating the stack at any layer N leaves layers 1..N
applied with the repo building and tests passing; git reset, stash and branch-switch are never
invoked on any path; at most 25 findings per layer are surfaced with the remainder deferred to
the ledger; and a fix replacing a plain statement with a nested comprehension is rejected by the
self-audit.
"""

from __future__ import annotations

import pytest

from agent_factory.af_clean.commit_stack import (
    DEFAULT_FINDINGS_CAP_PER_LAYER,
    LAYER_COMMENTS,
    LAYER_COVERED_UNREACHABLE_DELETIONS,
    LAYER_DEAD_IMPORT_CLEANUP,
    LAYER_ORDER,
    LAYER_SAME_JOB_CONSOLIDATIONS,
    ForbiddenGitOperation,
    Layer,
    LayerChange,
    apply_commit_stack,
    build_layers,
    self_audit_change,
    unwind_to_layer,
)


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _change(rule: str = "r", density: float = 1.0, before: str = "x = 1", after: str = "x = 2") -> LayerChange:
    return LayerChange(finding_rule=rule, before=before, after=after, density=density)


# --------------------------------------------------------------------------- layer order (B25)

def test_layer_order_is_fixed_with_dead_import_cleanup_last():
    assert LAYER_ORDER == (
        LAYER_COMMENTS,
        LAYER_COVERED_UNREACHABLE_DELETIONS,
        LAYER_SAME_JOB_CONSOLIDATIONS,
        "behavior_adjacent_simplifications",
        LAYER_DEAD_IMPORT_CLEANUP,
    )
    assert LAYER_ORDER[-1] == LAYER_DEAD_IMPORT_CLEANUP


def test_build_layers_emits_all_five_layers_in_order_even_when_input_dict_is_out_of_order():
    changes_by_layer = {
        LAYER_DEAD_IMPORT_CLEANUP: [_change("dead-import")],
        LAYER_COMMENTS: [_change("stale-comment")],
    }
    layers = build_layers(changes_by_layer)
    assert [layer.name for layer in layers] == list(LAYER_ORDER)


def test_unknown_layer_key_is_ignored_not_smuggled_in():
    layers = build_layers({"not_a_real_layer": [_change()]})
    assert sum(len(layer.changes) for layer in layers) == 0


# --------------------------------------------------------------------------- B45 cap + defer

def test_cap_defaults_to_25_and_defers_the_remainder():
    assert DEFAULT_FINDINGS_CAP_PER_LAYER == 25
    many = [_change(rule=f"r{i}", density=float(i)) for i in range(30)]
    layers = build_layers({LAYER_COMMENTS: many})
    comments_layer = layers[0]
    assert len(comments_layer.changes) == 25
    assert len(comments_layer.deferred) == 5


def test_cap_selects_by_descending_density_deferring_the_lowest():
    many = [_change(rule=f"r{i}", density=float(i)) for i in range(27)]
    layers = build_layers({LAYER_COMMENTS: many}, cap_per_layer=25)
    kept_densities = {c.density for c in layers[0].changes}
    deferred_densities = {c.density for c in layers[0].deferred}
    assert kept_densities == {float(i) for i in range(2, 27)}
    assert deferred_densities == {0.0, 1.0}


def test_findings_within_cap_are_not_deferred():
    few = [_change(rule=f"r{i}") for i in range(5)]
    layers = build_layers({LAYER_COMMENTS: few})
    assert len(layers[0].changes) == 5
    assert layers[0].deferred == ()


# --------------------------------------------------------------------------- B26 self-audit

def test_self_audit_rejects_plain_statement_replaced_by_nested_comprehension():
    change = LayerChange(
        finding_rule="flatten-loop",
        before=(
            "result = []\n"
            "for row in matrix:\n"
            "    for x in row:\n"
            "        result.append(x)\n"
        ),
        after="result = [y for y in [x for x in row] for row in matrix]\n",
    )
    ok, findings = self_audit_change(change)
    assert ok is False
    assert len(findings) == 1
    assert findings[0].rule == "over-collapsed-procedure"


def test_self_audit_accepts_a_plain_statement_replaced_by_a_flat_comprehension():
    change = LayerChange(
        finding_rule="flatten-loop",
        before="result = []\nfor x in items:\n    result.append(x * 2)\n",
        after="result = [x * 2 for x in items]\n",
    )
    ok, findings = self_audit_change(change)
    assert ok is True
    assert findings == ()


def test_self_audit_ignores_unparseable_source_rather_than_blocking():
    change = LayerChange(finding_rule="r", before="not ( python", after="also not ) python")
    ok, findings = self_audit_change(change)
    assert ok is True


def test_build_layers_drops_self_audit_rejected_changes_from_what_is_applied():
    rejected = LayerChange(
        finding_rule="flatten-loop",
        before="for x in items:\n    total.append(x)\n",
        after="total = [y for y in [x for x in items]]\n",
        density=10.0,
    )
    fine = _change(rule="ok", density=1.0)
    behavior_layer_name = "behavior_adjacent_simplifications"
    layers = build_layers({behavior_layer_name: [rejected, fine]})
    layer = next(layer for layer in layers if layer.name == behavior_layer_name)
    assert rejected not in layer.changes
    assert fine in layer.changes
    assert len(layer.self_audit_rejections) == 1


# --------------------------------------------------------------------------- prefix truncation + unwind

def test_truncating_at_layer_n_leaves_layers_1_to_n_applied_when_repo_builds_and_tests_pass(tmp_path):
    layers = [
        Layer(name=LAYER_COMMENTS, changes=(_change("c1"),)),
        Layer(name=LAYER_COVERED_UNREACHABLE_DELETIONS, changes=(_change("c2"),)),
        Layer(name=LAYER_SAME_JOB_CONSOLIDATIONS, changes=(_change("c3"),)),
    ]
    git_argvs: list[list[str]] = []
    shas = iter(["sha-a", "sha-b", "sha-c"])

    def git_runner(argv, cwd):
        git_argvs.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return _Proc(stdout=next(shas) + "\n")
        return _Proc(0)

    # Layer 2 (index 1) is where validation breaks -- layer 3 must never even be attempted.
    validated_layers = []

    def validate_fn(repo):
        validated_layers.append(len(validated_layers))
        return validated_layers[-1] < 1  # first call True, second call False

    result = apply_commit_stack(
        layers,
        tmp_path,
        git_runner=git_runner,
        apply_layer_files=lambda layer: None,
        validate_fn=validate_fn,
    )

    assert result.applied_layers == [LAYER_COMMENTS]
    assert result.failed_layer == LAYER_COVERED_UNREACHABLE_DELETIONS
    assert result.truncated is True
    # Layer 3 was never committed at all -- prefix truncation, not "apply everything then fix up".
    commit_messages = [argv for argv in git_argvs if argv[:2] == ["git", "commit"]]
    assert len(commit_messages) == 2


def test_forbidden_git_operations_are_never_invoked_on_any_path_including_failure(tmp_path):
    layers = [
        Layer(name=LAYER_COMMENTS, changes=(_change("c1"),)),
    ]
    git_argvs: list[list[str]] = []

    def git_runner(argv, cwd):
        git_argvs.append(argv)
        if argv[:2] == ["git", "rev-parse"]:
            return _Proc(stdout="sha-a\n")
        return _Proc(0)

    apply_commit_stack(
        layers,
        tmp_path,
        git_runner=git_runner,
        apply_layer_files=lambda layer: None,
        validate_fn=lambda repo: False,  # force the revert path
    )

    subcommands = {argv[1] for argv in git_argvs if len(argv) > 1}
    assert subcommands.isdisjoint({"reset", "stash", "checkout", "switch"})
    assert "revert" in subcommands


def test_apply_commit_stack_raises_forbidden_git_operation_rather_than_silently_running_it(tmp_path):
    def git_runner(argv, cwd):
        return _Proc(0)

    # Directly exercise the guard: a git_runner call requesting "reset" must be refused before
    # it ever reaches the injected runner.
    from agent_factory.af_clean.commit_stack import _run_git

    with pytest.raises(ForbiddenGitOperation):
        _run_git(git_runner, ["git", "reset", "--hard"], tmp_path, [])
    with pytest.raises(ForbiddenGitOperation):
        _run_git(git_runner, ["git", "stash"], tmp_path, [])
    with pytest.raises(ForbiddenGitOperation):
        _run_git(git_runner, ["git", "checkout", "main"], tmp_path, [])
    with pytest.raises(ForbiddenGitOperation):
        _run_git(git_runner, ["git", "switch", "main"], tmp_path, [])


def test_unwind_to_layer_reverts_trailing_commits_in_reverse_order_only():
    git_argvs: list[list[str]] = []

    def git_runner(argv, cwd):
        git_argvs.append(argv)
        return _Proc(0)

    reverted = unwind_to_layer(
        "/tmp/repo", ["sha-1", "sha-2", "sha-3", "sha-4"], keep_n=2, git_runner=git_runner
    )

    # Only the trailing suffix (3, 4) is touched, head-first (4 before 3) -- never the prefix.
    assert reverted == ["sha-4", "sha-3"]
    revert_argvs = [argv for argv in git_argvs if argv[1] == "revert"]
    assert [argv[-1] for argv in revert_argvs] == ["sha-4", "sha-3"]


def test_unwind_to_layer_cannot_touch_an_isolated_middle_commit():
    """B25: the affordance is prefix truncation, never an arbitrary middle-layer revert."""
    git_argvs: list[list[str]] = []

    def git_runner(argv, cwd):
        git_argvs.append(argv)
        return _Proc(0)

    # Asking to keep everything except the middle commit (sha-2) is not an operation this API
    # exposes at all: keep_n only ever describes a PREFIX length.
    reverted = unwind_to_layer("/tmp/repo", ["sha-1", "sha-2", "sha-3"], keep_n=3, git_runner=git_runner)
    assert reverted == []
    assert git_argvs == []
