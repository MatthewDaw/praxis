"""Post-cutover byte-level characterization for the P-8 CLI module split."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest


HELP_SHA256 = {
    "__root__": "49b1088bd2c15b991f7eed080cdff3bf92744d0256e38168bd4daa74dede8bb0",
    "adjudicate-run": "4037229e386f8e7980b0a8146f2e0dc5033e71975bf8fee85deb47322109314a",
    "adopt-idea": "2634a0014387d7fd901ae5fcc7c237ebf3af8af4e1abf652235f3a0f70554e18",
    "backlog": "d02ef7e0961d5bcfcfb24a645e4d0b8f2b7b9f83518ea58c37de7225860c41ef",
    "claim-idea": "41804fa5e205a4861c1a69984c742d233d45e2038bcbf12d1430bbef0b8952d5",
    "complete-run": "f613cc9720df319bcc9f4330fccef8c8cad93753c588591937e04a3e1d72674d",
    "create-artifact": "b15ffecccf40ae79cbabc8313ebeb21c71673ab97d62aa6a47c4e87a5a489450",
    "create-experiment": "d125db6015378c7d440bbab54a1397f406d231965c465f1cd43def8f985a52e6",
    "create-lineage": "9f477ff891911f2b5079ec6873145df8f915becb54f44431da6d2a375d9dc763",
    "create-run": "460088a729f1fee07f84b9b1f4d2620e6ba68ced22cdc059efd086cb1e455917",
    "export-runs": "52660318365b962621bbc97ffa271ad85005f9acce0451e66343682fbee9103a",
    "finalize": "adf6938372209c1bddb946fa6138896a94f0a24f1c6d666ee1a611f57862977f",
    "heartbeat-idea-claim": "753684fe29ab3d318ad3883837969e06e0a6101c4eb3f444ecbf4a3fdc616638",
    "import-historical-archive": "e54eb4d7057b9c4aabcd2d1ce19c1dde4a0b458ccc535945f962f246264c00be",
    "import-historical-evidence-freeze": "2bb3fb2066c5f7f083170bc1e98b8e0f5efeda3f1f7b9dd50a6a8d53f3dad0b2",
    "import-historical-ledger": "66bce2728d26ea2690ea9f95450c501ca4e84294a6b78e77183c6edce285a453",
    "invalidate-adoption": "68b91014ebd4c994f4da8542daecff72f2fa60cfae6dc0d1e99090ba84d2fcbe",
    "park-idea": "261aec1db2d276d9aba326e3d6b4328b16ff090ce713cb25fc3c208dedc46bbb",
    "readback": "0ce9867c6d8e1525efb5b5c48115b010afa9eaaa8b09f6409859835f8378c395",
    "register-idea": "4c7ba87d2f7cb2a53181e1faee8a92ba9ed91370a0d8bac29bf22ae48671e40f",
    "register-model": "df4ce04d71e1aa36310fa0ea51ab8bd0bbaa8fafd7e52b04b03fbccf4e42725a",
    "registry-status": "5ffd0dc385e998687d91f2b8c0da2400dc32d6ad040bf495b2674c43aad7a1ea",
    "reject-idea": "40bb05d4037ee14dff2767a7bb8fdd8e8d11f7fc93dfd9f049e84e441d641437",
    "rejection-memory": "670b013601aed626b162e6e728e4b004cc432a3a15c79ccc510426173993e8c5",
    "reopen-idea": "8cf133c31712c5eac00249d57dc2db6ce20a1aeffdff4c48e98f955acc48cd21",
    "resolve-citation": "df449b4d5350dc0f892dcbb1d74cec645052ca97c43e48285e1873edbefacdb4",
    "retriable-ideas": "7dac22812c01eb8dd04064eb0c397b8acb155188d2204b4bfe13232303f203ff",
    "seed-campaign": "0c467694733a9cfa120db213494c330afff4e15ddbfd6302c35ac8ba3355de3c",
}

ERROR_SHA256 = {
    "__root__": "212679c6b42e3c8520677b1b70171c582eceaf3abcf7076115d7ed09a49c735b",
    "adjudicate-run": "e044c8fbb511f2943f99aeb4dd2f616e262c98a308bfd98c6b55389e075a249b",
    "adopt-idea": "511283fe14ca352e9a21f3d4eeb4a2222b728b63b7b251f9ac16db3808e0f472",
    "backlog": "5b316447b4dce9de3f1f330f8b7d0d7a337cfdaadb9503bdeca936754b9c6959",
    "claim-idea": "4a3a51a85f808b5c67e5f8bace9ef2d3e8007a1b058d1de2dc0cc3766ba60188",
    "complete-run": "16b0f0a476513bf9936c434e43dc4757c231b1f6abc1b517f67b852b269654a2",
    "create-artifact": "2105c6af4d3cb197e0727abe978c9528cd2e0986b3e5241eb564be0d011e9fc3",
    "create-experiment": "ed77048293a0a890fa7d507c92131e397bf59f61c2bde1c1f013f45a4d0c9225",
    "create-lineage": "e65e501fa76328ad1d4902b7b358ec60a122ebab22d29f9f56085fa82e356472",
    "create-run": "e52ab93e3916bce50de37df43b6a0d89a0f291a710f43b91b93d11daf49fc98c",
    "export-runs": "d9635e275fbb92fb21188999ea3d365efff9eabca5b633c508848f3450ae4401",
    "finalize": "c50edf2cc82e4cb2e1c7432b21e187f0aee27ca9f12c16cde299544c576a7745",
    "heartbeat-idea-claim": "aa161b24b83fb1132a3a0436f00a4068e2f0fdd4da0744382c9eadede0226574",
    "import-historical-archive": "91d7b4a1e10d34b2d182824393b724e0c26540dcb049e5d89c985942166d89bf",
    "import-historical-evidence-freeze": "519021e6e361fb619ecc3f8fc29fdf6af16636e2e11de2f3890c2ad3d133fce4",
    "import-historical-ledger": "0a12dcbe947be17645c29235ef718cd104702f47eab0632668f25d38591f67cf",
    "invalidate-adoption": "b815324f9f0d8800d5b8326268665f30f45b7ebdaeda697b8f0ed6177c743bbd",
    "park-idea": "5cc4086597d65fbe311056f192b047f70f5fe62e1c2d7fda055000a14df7b3b5",
    "readback": "154b168bb86bd61b73c5cd78f8e23736034c8e84a1c5e5fae96226dae7bcf9c9",
    "register-idea": "359fd63106367174527342ec0fe30c8bb27d4752b80797285f329ef0eebcb90a",
    "register-model": "02d329992b3529d9382c75ef9b2a10816088f85e387eede3203163e1b52d9fe2",
    "registry-status": "3f862733237cfca14139708658e2067b8085824f6436a54b757d58f4de2b6108",
    "reject-idea": "f32d3a1b8ef3794994dc903ae4d5c7615806e0a599e15303ca321d2319dc742d",
    "rejection-memory": "a249853b7642526e65f9c7e003b12e50f1f687b5138639f4f0fedfe716637d82",
    "reopen-idea": "37b98cac3a9fa6be3f9fa774e340bd5ba3a682c5ae3e279f85a0dee969d060d7",
    "resolve-citation": "3a3c45fbf9a73cfe050ec4866d8cd44fcf25ae2ab5f130cd03d70cd2312a1623",
    "retriable-ideas": "693bf9557580a575ef2845029f78ee0545ffbd4f25ce9f7a9e364a892df4d1d3",
    "seed-campaign": "5cf07fa4ec10f62a632beb1ba42f3c3f80e353c0889f70b0828400d9ee8302be",
}

PUBLIC_NAME_SHA256 = {
    "knowledge.ml_registry.cli": "919c1ce3f38cda40c1da9bcbfd0726650c9d517117bae969cc7032546350345d",
    "knowledge.ml_registry.portfolio_cli": "e35685022ab87fb046a616f820527b1a547d801d9cb82b4ef85c54ecb3c17a76",
    "knowledge.ml_registry.manifests_cli": "f7d03d28cb7438edf013aae36d9f21142ffc1a93b9d16d9f283f43055e030dbb",
}

PORTFOLIO_HELP_SHA256 = {
    "__root__": "7c3e0c444d3dba89a3467e37f0cd6c38adcb99e914e335a616faa2041b932218",
    "run": "45f3c7cc0d603670d79ae522132c9fe0c59f629a4cdc5075b26dae5db06bdc0c",
    "status": "1f4a0e759ddd7e89fa1d6b0158f115e00b3afca85023487c395bf5b3439f7edc",
    "stop": "292e2ba0981ea95050f8c7ead37cf4157f0cd4bc197e37c35e3428bef3e257a7",
    "resume": "fb19ffd731f0a2994e90062569c707e1e807bd25cd9b35275ccaf7d6d2260aa0",
    "explain": "f5d3601d66db15246649b24eac30d7c440634136cd837fcae67c4a0ca2c65669",
}


def _run(module: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "COLUMNS": "80", "PRAXIS_DB_DISABLED": "1"}
    return subprocess.run(
        [sys.executable, "-m", module, *arguments],
        capture_output=True, text=True, env=env, check=False,
    )


@pytest.mark.parametrize("command", HELP_SHA256)
def test_every_registry_help_surface_matches_post_cutover_golden(command: str) -> None:
    arguments = ("--help",) if command == "__root__" else (command, "--help")
    result = _run("knowledge.ml_registry.cli", *arguments)
    assert (result.returncode, result.stderr) == (0, "")
    assert hashlib.sha256(result.stdout.encode()).hexdigest() == HELP_SHA256[command]


@pytest.mark.parametrize("command", ERROR_SHA256)
def test_every_registry_missing_argument_surface_matches_post_cutover_golden(command: str) -> None:
    arguments = () if command == "__root__" else (command,)
    result = _run("knowledge.ml_registry.cli", *arguments)
    assert (result.returncode, result.stdout) == (2, "")
    assert hashlib.sha256(result.stderr.encode()).hexdigest() == ERROR_SHA256[command]


@pytest.mark.parametrize("command", PORTFOLIO_HELP_SHA256)
def test_every_portfolio_operator_help_surface_matches_cutover_golden(command: str) -> None:
    arguments = ("--help",) if command == "__root__" else (
        "--config", "fixture.json", command, "--help",
    )
    result = _run("knowledge.ml_registry.cli.portfolio", *arguments)
    assert (result.returncode, result.stderr) == (0, "")
    assert hashlib.sha256(result.stdout.encode()).hexdigest() == PORTFOLIO_HELP_SHA256[command]


@pytest.mark.parametrize(
    ("module", "arguments", "code", "stdout"),
    [
        ("knowledge.ml_registry.manifests_cli", ("--help",), 3,
         '{"ok": false, "error": "validation", "message": "path-owned manifests retired; use the canonical registry CLI"}\n'),
    ],
)
def test_standalone_facade_transcripts_are_unchanged(
    module: str, arguments: tuple[str, ...], code: int, stdout: str,
) -> None:
    result = _run(module, *arguments)
    assert (result.returncode, result.stdout, result.stderr) == (code, stdout, "")


def test_legacy_imports_and_split_modules_resolve_to_the_same_objects() -> None:
    from knowledge.ml_registry import manifests_cli, portfolio_cli
    from knowledge.ml_registry.cli import (
        _json_arg, _load_mutate_save, _lock_timeout_seconds, load_ledger_rows, main,
    )
    from knowledge.ml_registry.cli import manifests, portfolio, registry

    assert main is registry.main
    assert load_ledger_rows is registry.load_ledger_rows
    assert _json_arg is registry._json_arg
    assert _load_mutate_save is registry._load_mutate_save
    assert _lock_timeout_seconds is registry._lock_timeout_seconds
    assert portfolio_cli.main is portfolio.main
    assert manifests_cli.main is manifests.main
    assert portfolio_cli._parser is portfolio._parser
    assert manifests_cli._parser is manifests._parser


@pytest.mark.parametrize("module_name", PUBLIC_NAME_SHA256)
def test_every_former_public_import_name_matches_post_cutover_golden(module_name: str) -> None:
    import importlib

    module = importlib.import_module(module_name)
    split_modules = {"registry", "portfolio", "manifests"}
    payload = "\n".join(sorted(
        name for name in vars(module) if not name.startswith("_") and name not in split_modules
    )) + "\n"
    assert hashlib.sha256(payload.encode()).hexdigest() == PUBLIC_NAME_SHA256[module_name]
