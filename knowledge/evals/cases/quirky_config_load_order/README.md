# quirky_config_load_order

## Matthew pipeline handoff

This eval case simulates **post-resolution promotion** of `cand_9` after a human
keeps the early-boot lesson and rejects the rival.

| Field | Expected value |
|-------|----------------|
| **Winning candidate id** | `cand_9` |
| **Title** | experimental_options Before Config Load |
| **Provenance** | `logs/nushell_contrib_20260611.jsonl:56` |
| **Contradictions** | `["cand_16"]` |

## Rival lesson (pipeline output only — do NOT inject into eval)

Matthew's dedup/contradiction pipeline should surface **both** candidates from
distillation, but only the winning lesson belongs in `direct_to_graph`:

| Field | Rival `cand_16` |
|-------|-----------------|
| **Title** | Experimental Flags in config.nu |
| **Content** | For most experimental nushell features, enable flags under the `$env.config` experimental section in `config.nu` so they persist across sessions. |
| **Provenance** | `logs/nushell_contrib_20260610.jsonl:44` |

The dashboard Act 2 demo resolves this pair in
[`frontend/mock_data.py`](../../../frontend/mock_data.py) and
[`docs/monica/DEMO_SCRIPT.md`](../../../docs/monica/DEMO_SCRIPT.md).
