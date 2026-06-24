"""Write-time extraction of atomic (subject, attribute, value) claims.

The front of the structural contradiction path. Each incoming fact is decomposed
into atomic claims, each tagged ``functional`` (single-valued for its subject) or
not. The detector (``ClaimConflictDetector``) then flags a contradiction only when
two facts share a subject + functional attribute with incompatible values — so the
quality of this extraction is what makes detection precise.

``ClaimExtractionJudge`` mirrors ``AspectJudge``/``ConflictJudge``: structured
output over the LLM seam, replayed offline from a ``VerdictCassette`` (keyed by the
fact text), graceful skip when no source. ``ClaimExtractor`` is the write step that
attaches the extracted claims to the decision (persisted to the ``claims`` table).

Precision-first: extraction returns no claims rather than guesses when uncertain,
so the detector simply has nothing to flag (a missed conflict beats a false one).
"""

from __future__ import annotations

import json

from knowledge.knowledge_graph.knowledge_graph_def import Claim
from knowledge.knowledge_graph.write_policy.parent_write_step import WriteStep
from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm
from knowledge.llm.verdict_cassette import VerdictCassette

_PROMPT = (
    "Decompose the NOTE into atomic factual claims as (subject, attribute, value, "
    "functional). Extract ONLY what THIS note asserts; do not reuse any wording from "
    "examples elsewhere in this prompt.\n"
    "- subject: the canonical thing the claim is about. Attach a property to the thing "
    "it inherently describes, not to the actor (an invention's year -> the invention). "
    "For a rule of the form 'use X for Y' / 'Y must be X', the subject is Y (the thing "
    "governed) and the value is X. Config, infrastructure, and policy facts count too "
    "(a storage timezone, a rate limit, an indentation style, a required tool). Use a "
    "stable canonical name, and the SAME name for a subset as for the whole (a rule "
    "about one table's timestamps still uses subject 'timestamps').\n"
    "- attribute: the specific property in question (e.g. 'invention year', "
    "'indentation style', 'storage timezone').\n"
    "- value: the asserted value, normalized (years and numbers as bare numbers).\n"
    "- functional: true if the attribute is single-valued for that subject (a year, a "
    "timezone, an indentation style) so a DIFFERENT value would be a contradiction; "
    "false if it is naturally multi-valued (a person's discoveries, list members) so "
    "values coexist.\n"
    "Emit only claims the NOTE actually makes; keep attributes specific so unrelated "
    "facts don't collide. Return an empty list if the note makes no checkable claim.\n"
    "NOTE: {note}"
)

# A dedicated, single-task stance classifier. Folding this into the big extraction
# prompt diluted it (poor recall on paraphrases); a focused call that does ONE thing
# — map the note onto a controlled tradeoff axis + pole, or 'none' — recalls reliably,
# which is what lets two differently-worded notes collide on the same axis slot.
_STANCE_PROMPT = (
    "Does the NOTE advocate a side of any of these software-engineering TRADEOFF "
    "AXES? Decide by MEANING, not wording — paraphrases, metaphors, and indirect "
    "phrasings about the same tradeoff map to the SAME axis. Set ``axis`` to the best "
    "match and ``pole`` to the side favored (one of that axis's two poles), or set "
    "``axis`` to 'none' if the note expresses no such preference.\n"
    "{axes}"
    "Examples (note -> axis, pole):\n"
    "  'squeeze every cycle even if the code gets gnarly' -> axis:performance-vs-readability, performance\n"
    "  'keep it easy for a newcomer to follow even if slower' -> axis:performance-vs-readability, readability\n"
    "  'keep it readable even if it leaves speed on the table' -> axis:performance-vs-readability, readability\n"
    "  (note: 'speed' here is runtime performance, NOT the testing-rigor-vs-speed axis, "
    "which is only about skipping/keeping TESTS)\n"
    "  'instrument everything, you can't have too much insight' -> axis:logging-verbose-vs-minimal, verbose\n"
    "  'emit as little chatter as possible' -> axis:logging-verbose-vs-minimal, minimal\n"
    "  'write a guide explaining each module' -> axis:documentation-explicit-vs-self-documenting, explicit\n"
    "  'lean on expressive names so code needs no docs' -> axis:documentation-explicit-vs-self-documenting, self-documenting\n"
    "  'halt loudly the moment something looks wrong' -> axis:error-fail-fast-vs-fail-safe, fail-fast\n"
    "  'swallow the hiccup and return a safe default' -> axis:error-fail-fast-vs-fail-safe, fail-safe\n"
    "  'hoist shared logic into a reusable layer' -> axis:abstraction-vs-directness, abstraction\n"
    "  'spell each path out inline, copy-paste beats machinery' -> axis:abstraction-vs-directness, directness\n"
    "  'test thoroughly before shipping' -> axis:testing-rigor-vs-speed, testing-rigor\n"
    "  'ship fast, skip the exhaustive tests' -> axis:testing-rigor-vs-speed, speed\n"
    "  'always indent with tabs, spaces forbidden' -> axis:indentation-tabs-vs-spaces, tabs\n"
    "  'always indent with spaces, tabs forbidden' -> axis:indentation-tabs-vs-spaces, spaces\n"
    "  'keep every knob in one central place' -> axis:config-centralized-vs-colocated, centralized\n"
    "  'put each setting beside the code that reads it' -> axis:config-centralized-vs-colocated, colocated\n"
    "  'reach for a battle-tested library' -> axis:dependency-library-vs-diy, library\n"
    "  'roll your own to avoid the dependency' -> axis:dependency-library-vs-diy, diy\n"
    "NOTE: {note}"
)

# Controlled vocabulary of tradeoff axes for STANCE claims. Closed-ish list forces
# differently-phrased notes about the same tradeoff onto the SAME axis subject (the
# canonicalization the recall step needs); the model may fall back to a free
# 'axis:<a>-vs-<b>' when nothing fits. Each entry is (axis-subject, pole_a, pole_b).
AXIS_VOCAB: list[tuple[str, str, str]] = [
    ("axis:performance-vs-readability", "performance", "readability"),
    ("axis:abstraction-vs-directness", "abstraction", "directness"),
    ("axis:testing-rigor-vs-speed", "testing-rigor", "speed"),
    ("axis:dependency-library-vs-diy", "library", "diy"),
    ("axis:config-centralized-vs-colocated", "centralized", "colocated"),
    ("axis:error-fail-fast-vs-fail-safe", "fail-fast", "fail-safe"),
    ("axis:documentation-explicit-vs-self-documenting", "explicit", "self-documenting"),
    ("axis:logging-verbose-vs-minimal", "verbose", "minimal"),
    ("axis:indentation-tabs-vs-spaces", "tabs", "spaces"),
]
_AXES_BLOCK = "".join(
    f"      {subj}  (poles: {a} | {b})\n" for subj, a, b in AXIS_VOCAB
)
# Enum of axis subjects (+ "none") for the schema-constrained stance field. The
# enum is what actually forces canonicalization — the model must pick a listed
# axis, so two differently-phrased notes on the same tradeoff land on one slot.
_AXIS_ENUM = [subj for subj, _, _ in AXIS_VOCAB] + ["none"]

# Structured output: atomic claims plus an enum-constrained stance.
_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "claims",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "attribute": {"type": "string"},
                            "value": {"type": "string"},
                            "functional": {"type": "boolean"},
                        },
                        "required": ["subject", "attribute", "value", "functional"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["claims"],
            "additionalProperties": False,
        },
    },
}

# Dedicated stance schema: axis constrained to the controlled vocab (or "none").
_STANCE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "stance",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "axis": {"type": "string", "enum": _AXIS_ENUM},
                "pole": {"type": "string"},
            },
            "required": ["axis", "pole"],
            "additionalProperties": False,
        },
    },
}


class ClaimExtractionJudge:
    """Extracts atomic (subject, attribute, value) claims from a note (or None to skip)."""

    def __init__(
        self, llm: Llm | None = None, cassette: VerdictCassette | None = None
    ) -> None:
        self.llm = llm
        self.cassette = cassette

    def extract(self, note: str) -> list[Claim] | None:
        """Claims for ``note`` if a source is available; None to skip (no cassette, no llm)."""
        # Two focused calls: free-form atomic claims, plus a single-task stance
        # classifier (enum-constrained axis). Key the cassette on the exact rendered
        # prompts (both calls), so editing either prompt or the axes vocabulary is a
        # clean miss, not a stale replay.
        claim_prompt = _PROMPT.format(note=note)
        stance_prompt = _STANCE_PROMPT.format(axes=_AXES_BLOCK, note=note)
        if self.cassette is not None:
            payload = f"{claim_prompt}\n||\n{stance_prompt}"
            raw = self.cassette.verdict(
                payload, lambda: self._compute(claim_prompt, stance_prompt)
            )
            return self._to_claims(raw)
        if self.llm is not None:
            return self._to_claims(self._compute(claim_prompt, stance_prompt))
        return None  # no source -> skip

    def _compute(self, claim_prompt: str, stance_prompt: str) -> dict:
        claims_raw = self.llm.complete(
            [ChatMessage(role="user", content=claim_prompt)],
            response_format=_SCHEMA,
        )
        stance_raw = self.llm.complete(
            [ChatMessage(role="user", content=stance_prompt)],
            response_format=_STANCE_SCHEMA,
        )
        return {"claims": json.loads(claims_raw)["claims"], "stance": json.loads(stance_raw)}

    @staticmethod
    def _to_claims(raw: dict) -> list[Claim]:
        out: list[Claim] = []
        for c in raw.get("claims", []):
            try:
                out.append(
                    Claim(
                        subject=str(c["subject"]),
                        attribute=str(c["attribute"]),
                        value=str(c["value"]),
                        functional=bool(c["functional"]),
                    )
                )
            except (KeyError, TypeError):
                continue  # precision-first: drop a malformed claim, don't fail the write
        # A stance on a tradeoff axis becomes a functional claim keyed on the axis
        # (subject) so two opposing stances collide on one slot and the detector flags
        # them. axis == "none" (or missing) -> the note takes no position.
        stance = raw.get("stance") or {}
        axis = str(stance.get("axis", "none"))
        pole = str(stance.get("pole", "")).strip()
        if axis and axis != "none" and pole:
            out.append(Claim(subject=axis, attribute="stance", value=pole, functional=True))
        return out


class ClaimExtractor(WriteStep):
    """Attach extracted (subject, attribute, value) claims to the incoming note."""

    consumes_candidates = False  # extracts from the new note; needs no existing candidates

    def __init__(self, judge: ClaimExtractionJudge | None = None) -> None:
        self.judge = judge

    def apply(self, decision: WriteDecision) -> None:
        if self.judge is None or decision.dropped:
            return
        try:
            claims = self.judge.extract(decision.text)
        except Exception:
            # Extraction unavailable (e.g. no API key / network) — best-effort, leave
            # the note without claims rather than failing the write.
            return
        if claims:
            decision.claims = list(claims)
