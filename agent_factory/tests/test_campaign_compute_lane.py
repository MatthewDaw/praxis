"""A campaign must not run on a host that cannot satisfy the compute it declares.

THE GAP. `knowledge.ml_registry.preflight` asks seven questions before a campaign is dispatched --
STATUS, COMPLETE, LEDGER, SEED, CORPUS, DISPATCH, STRUCTURE -- and every one of them is about
corpora and bookkeeping. None is about the hardware.

That matters because of how this fails. This box has no GPU: no driver, no /dev/nvidia*, torch
installed as the +cpu build. A GPU-declared campaign run here does NOT crash. ML code falls back to
CPU silently and by design, so the arm runs, produces numbers, and those numbers are recorded
against a campaign that claims to have measured a GPU regime. The output is a measurement of a
different thing, reported as real, with nothing anywhere marking it. A crash would have been kinder.

FOUND WHILE FIXING IT: the wrapper could not invoke its own engine at all. `preflight` was
generalized from a hardcoded CAMPAIGNS table to a versioned manifest, gaining required --manifest
and --project-root, and the wrapper kept passing --repo. Every invocation died with an argparse
usage dump and exit 2 -- a readiness gate that had not run since the refactor, which nothing
noticed because nothing had called it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_factory import host_capability

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "af-ml-campaign-preflight.sh"


def _manifest(tmp_path: Path, campaigns: list[dict]) -> Path:
    path = tmp_path / "manifest.json"
    base = {"space": "s", "model_id": "m", "ledger": "l.tsv",
            "corpus_probe": "print('OK none')", "arms_probe": "print('ARMS a')",
            "composing_module": "", "dispatch": "echo"}
    path.write_text(json.dumps({
        "schema_version": 1,
        "campaigns": [{**base, **c} for c in campaigns],
        "refused": {},
    }))
    return path


def _fake_engine(tmp_path: Path) -> Path:
    """A `uv` on PATH that records the delegated argv instead of running the real engine.

    The wrapper shells out via `uv run python -m knowledge.ml_registry.preflight`, so this is the
    seam. Recording argv is the point: the original bug was entirely about which flags got passed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "engine-argv.txt"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > {log}\n'
        'for a in "$@"; do case "$a" in --manifest) next=1;; *) '
        'if [ "${next:-0}" = 1 ]; then cp "$a" ' f'{tmp_path}/delegated-manifest.json' '; next=0; fi;; esac; done\n'
        'echo "PREFLIGHT fake RESULT READY exit=0 pass=7 fail=0 runnable_arms=1"\n'
        "exit 0\n"
    )
    uv.chmod(0o755)
    return log


def _run(tmp_path: Path, *args: str, gpu: bool = False) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"
    if gpu:
        env["AF_HOST_ASSUME_GPU"] = "1"
    else:
        env.pop("AF_HOST_ASSUME_GPU", None)
    return subprocess.run(["bash", str(WRAPPER), *args], capture_output=True, text=True,
                          env=env, timeout=300)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "project"
    d.mkdir()
    return d


# ------------------------------------------------------------------------------ the compute gap --

def test_a_gpu_campaign_is_refused_on_a_host_with_no_gpu(tmp_path: Path, repo: Path):
    """THE REGRESSION. Nothing used to ask, and the run would have looked entirely successful."""
    log = _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det", "device": "gpu"}])

    res = _run(tmp_path, "--repo", str(repo), "--manifest", str(mf), "--campaign", "det")

    assert res.returncode == 3, res.stdout + res.stderr
    assert "PREFLIGHT det COMPUTE FAIL device=gpu" in res.stdout
    assert "PREFLIGHT det RESULT REFUSED exit=3" in res.stdout
    assert not log.exists(), "a refused campaign must never reach the engine at all"


def test_the_refusal_carries_the_evidence_and_the_way_out(tmp_path: Path, repo: Path):
    _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det", "device": "gpu"}])

    res = _run(tmp_path, "--repo", str(repo), "--manifest", str(mf), "--campaign", "det")

    assert "/dev/nvidia* absent" in res.stdout or "torch" in res.stdout
    assert "AF_HOST_ASSUME_GPU" in res.stderr, "an operator must be told the override exists"
    assert "measurement of a regime that was never exercised" in res.stderr or \
           "never exercised" in res.stderr


def test_a_cpu_campaign_passes_compute_and_reaches_the_engine(tmp_path: Path, repo: Path):
    log = _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det", "device": "cpu"}])

    res = _run(tmp_path, "--repo", str(repo), "--manifest", str(mf), "--campaign", "det")

    assert res.returncode == 0, res.stdout + res.stderr
    assert "PREFLIGHT det COMPUTE PASS device=cpu" in res.stdout
    assert log.exists(), "a satisfiable campaign must still be checked by the engine"
    assert "--campaign\ndet" in log.read_text()


def test_a_manifest_that_declares_nothing_still_means_cpu(tmp_path: Path, repo: Path):
    """Back-compat: every existing manifest predates the field and must behave exactly as before."""
    _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det"}])

    res = _run(tmp_path, "--repo", str(repo), "--manifest", str(mf), "--campaign", "det")

    assert res.returncode == 0
    assert "PREFLIGHT det COMPUTE PASS device=cpu" in res.stdout


def test_one_refusal_does_not_stop_the_queue(tmp_path: Path, repo: Path):
    """A refused campaign is skipped and reported, never fatal — the others still get checked."""
    log = _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det", "device": "gpu"}, {"name": "assoc", "device": "cpu"}])

    res = _run(tmp_path, "--repo", str(repo), "--manifest", str(mf), "--all")

    assert "PREFLIGHT det RESULT REFUSED exit=3" in res.stdout
    assert "PREFLIGHT assoc COMPUTE PASS" in res.stdout
    assert log.exists(), "the satisfiable campaign must still have been delegated"
    delegated = log.read_text()
    assert "assoc" in delegated and "det" not in delegated.split("--campaign")[-1]
    assert res.returncode == 3, "the run as a whole is refused, but only after the rest was checked"


def test_the_override_lets_an_operator_proceed_deliberately(tmp_path: Path, repo: Path):
    _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det", "device": "gpu"}])

    res = _run(tmp_path, "--repo", str(repo), "--manifest", str(mf), "--campaign", "det", gpu=True)

    assert res.returncode == 0, res.stdout + res.stderr
    assert "COMPUTE PASS device=gpu" in res.stdout
    assert "operator asserts" in res.stdout, "the override must be recorded in the evidence"


def test_the_engine_never_sees_the_device_key(tmp_path: Path, repo: Path):
    """load_manifest refuses unknown keys, and it lives in the PROJECT tree that build workers are
    editing. The wrapper owns `device` and hands the engine the schema it has always had."""
    _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det", "device": "cpu"}])

    _run(tmp_path, "--repo", str(repo), "--manifest", str(mf), "--campaign", "det")

    delegated = json.loads((tmp_path / "delegated-manifest.json").read_text())
    assert delegated["campaigns"][0]["name"] == "det"
    assert "device" not in delegated["campaigns"][0]
    assert json.loads(mf.read_text())["campaigns"][0]["device"] == "cpu", "the source is untouched"


# ------------------------------------------------------------- the wrapper could not run at all --

def test_the_wrapper_passes_the_flags_the_engine_actually_requires(tmp_path: Path, repo: Path):
    """It was passing --repo to an engine that had grown --manifest/--project-root, so every single
    invocation exited 2 on an argparse usage dump."""
    log = _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det"}])

    _run(tmp_path, "--repo", str(repo), "--manifest", str(mf), "--campaign", "det")

    argv = log.read_text()
    assert "--manifest" in argv
    assert "--project-root" in argv
    assert "--repo" not in argv, "the engine has no such flag"


def test_a_misconfigured_wrapper_still_speaks_the_documented_contract(tmp_path: Path, repo: Path):
    """A queue runner parses stdout. An argparse dump on stderr and nothing on stdout is
    indistinguishable, to it, from the tool having said nothing."""
    _fake_engine(tmp_path)

    res = _run(tmp_path, "--repo", str(repo), "--manifest", str(tmp_path / "nope.json"),
               "--campaign", "det")

    assert res.returncode == 2
    assert res.stdout.startswith("PREFLIGHT ALL RESULT NOT_READY exit=2")
    assert "manifest_not_found" in res.stdout


def test_no_campaign_selected_is_a_usage_error_in_the_contract_shape(tmp_path: Path, repo: Path):
    _fake_engine(tmp_path)
    mf = _manifest(tmp_path, [{"name": "det"}])

    res = _run(tmp_path, "--repo", str(repo), "--manifest", str(mf))

    assert res.returncode == 2
    assert "PREFLIGHT ALL RESULT NOT_READY exit=2" in res.stdout


# ----------------------------------------------------------------------------- the probe itself --

def test_cpu_is_always_satisfiable():
    cap = host_capability.capability("cpu")
    assert cap.available is True
    assert "CPU" in cap.why()


def test_gpu_is_refused_without_positive_evidence(monkeypatch):
    monkeypatch.delenv("AF_HOST_ASSUME_GPU", raising=False)
    monkeypatch.setattr(host_capability, "glob", lambda pattern: [])
    monkeypatch.setattr(host_capability.shutil, "which", lambda name: None)
    monkeypatch.setattr(host_capability, "_run", lambda argv: (1, "no torch"))

    cap = host_capability.capability("gpu")

    assert cap.available is False
    assert "none" in cap.why()


def test_cannot_tell_answers_no(monkeypatch):
    """Absence of proof and proof of absence get the same treatment, deliberately: an unprovable
    GPU is exactly the state that produces a silent CPU fallback."""
    monkeypatch.delenv("AF_HOST_ASSUME_GPU", raising=False)
    monkeypatch.setattr(host_capability, "glob", lambda pattern: [])
    monkeypatch.setattr(host_capability.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(host_capability, "_run", lambda argv: (127, "OSError: boom"))

    assert host_capability.capability("gpu").available is False


def test_a_present_device_is_recognised(monkeypatch):
    """The gate has to stay passable on a machine that really does have one."""
    monkeypatch.delenv("AF_HOST_ASSUME_GPU", raising=False)
    monkeypatch.setattr(host_capability, "glob", lambda pattern: ["/dev/nvidia0"])
    monkeypatch.setattr(host_capability.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(host_capability, "_run",
                        lambda argv: (0, "GPU 0: NVIDIA A10G (UUID: GPU-abc)"))

    cap = host_capability.capability("gpu")
    assert cap.available is True
    assert "A10G" in cap.why()


def test_an_unknown_lane_is_an_error_not_a_silent_cpu(monkeypatch):
    with pytest.raises(ValueError):
        host_capability.capability("GPUU")
