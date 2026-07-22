"""MVP ingestor: a hardcoded prompt + one LLM call.

``synthesis`` hands the raw input to an injected ``llm`` callable under a single
hardcoded :data:`SPLIT_PROMPT`, then splits the response into one
:class:`Insight` per non-empty line. The LLM is injected (rather than imported)
so the harness can run offline with a deterministic fake.

Its only job is to break raw input into discrete ideas — independent of what is
already in the graph. Reconciling against existing knowledge (dedup, conflict,
confidence) happens downstream in ``graph.write`` (see ``Ingestor.ingest``), so
``synthesis`` never reads the graph.

If no ``llm`` is provided we can't run the real distillation, so ``synthesis``
falls back to a lightweight, deterministic cleanup of the raw text instead of
storing it verbatim. :func:`segment_passthrough` drops obvious document noise
(Markdown ``==`` headers, and the trailing ``See also`` / ``References`` /
``External links`` / ``Publications`` / ``Notes`` / ``Citations`` apparatus) and
splits the surviving prose into one atomic sentence per line — so an offline run
over a wiki-style article yields per-claim facts rather than raw chunks.
"""

from __future__ import annotations

import re
from typing import Callable

from knowledge.injestion.injestion_def import Insight
from knowledge.injestion.parent_injestor import Ingestor
from knowledge.injestion.table_linearizer import linearize_table
from knowledge.knowledge_graph.parent_knowledge_graph import KnowledgeGraph

# The one hardcoded instruction for the MVP. Only the raw input is appended by
# ``synthesis`` before the call — no graph state, by design (reconciliation with
# existing knowledge is ``graph.write``'s job, not the splitter's).
SPLIT_PROMPT = (
    "Break the input into discrete, self-contained ideas worth remembering — one "
    "per line. Each line MUST be a complete statement that stands on its own when "
    "read in isolation: it names its own subject and states one fact about it.\n"
    "- Resolve references using ONLY information present in the input (including "
    "the SOURCE line if given): replace pronouns and context-dependent phrases "
    "(\"this\", \"it\", \"the form\", a bare line/section number) with the concrete "
    "thing they refer to. Never invent details that are not in the input.\n"
    "- Do NOT output procedural instructions, headers, or sentence fragments that "
    "lack a subject (e.g. \"Enter on line 12.\"); fold such qualifiers into the "
    "fact they describe, or drop them.\n"
    "- Preserve concrete specifics (names, numbers, amounts, dates, form/line "
    "references).\n"
    "Do not merge distinct ideas, deduplicate, add commentary, or number the lines."
)

# An LLM is just a text-in/text-out callable here; keeps the dependency injectable.
LLM = Callable[[str], str]

# A Markdown/wiki section header line, e.g. ``== Career ==`` or ``=== Legacy ===``.
# Captures the heading text so we can decide whether the section is noise.
_HEADER_RE = re.compile(r"^\s*(={2,})\s*(.*?)\s*\1\s*$")

# Headings whose entire trailing section is apparatus, not knowledge: link lists,
# citation dumps, and bibliographies. Matching is case-insensitive on the heading
# text only (so ``== See also ==`` and ``=== Citations ===`` both qualify).
_NOISE_SECTIONS = {
    "see also",
    "references",
    "notes",
    "citations",
    "external links",
    "further reading",
    "bibliography",
    "publications",
    "sources",
}

# A citation/bibliography entry that slips through as a standalone line, e.g.
# ``Chisholm, Hugh, ed. (1911). "Volta, Alessandro" . Encyclopædia Britannica``.
_CITATION_RE = re.compile(r",\s*ed\.\s*\(\d{4}\)|OCLC\s*\d|Wayback Machine|Archived\b")

# Sentence boundary: a period/question/exclamation mark followed by whitespace and
# a capital/quote/digit. Abbreviations like ``H. B.`` keep their following token
# lower/initialed, so this avoids the most common false splits without a parser.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\d])")

# A line that is only a chemical half-reaction or formula fragment (e.g.
# ``Zn → Zn2+ + 2e−``) — not an atomic English claim.
_FORMULA_RE = re.compile(r"^[^a-zA-Z]*[A-Z][a-z]?\d?.{0,40}[→+−=].{0,40}$")


def _is_noise_line(line: str) -> bool:
    """True for lines that are document scaffolding rather than a claim."""
    if not line:
        return True
    if _HEADER_RE.match(line):  # a bare ``== Header ==`` line
        return True
    if _CITATION_RE.search(line):
        return True
    if _FORMULA_RE.match(line):
        return True
    return False


def segment_passthrough(raw_input: str) -> list[str]:
    """Clean a raw document into atomic, noise-free candidate facts (no LLM).

    Drops trailing apparatus sections wholesale (``See also`` onward), strips
    standalone Markdown headers and citation/list lines, and splits the surviving
    paragraphs into one sentence per line. Order is preserved; nothing is
    deduplicated (that is ``graph.write``'s job).
    """
    # Table branch: a markdown/CSV/key:value block has no concept of "sentences",
    # so the splitter below would mangle it. Linearize rows deterministically
    # instead — one self-contained fact per row (loss point A, offline path).
    table_facts = linearize_table(raw_input)
    if table_facts:
        return table_facts
    return _segment_prose(raw_input)


def _segment_prose(raw_input: str) -> list[str]:
    """Segment non-tabular prose into atomic, noise-free sentences (the table check
    is the caller's — this is the prose-only tail of :func:`segment_passthrough`)."""
    facts: list[str] = []
    skipping = False  # inside a trailing noise section
    for raw_line in raw_input.splitlines():
        line = raw_line.strip()
        header = _HEADER_RE.match(line)
        if header:
            level, title = header.group(1), header.group(2).strip().lower()
            if title in _NOISE_SECTIONS:
                skipping = True  # this section and its sub-sections are apparatus
            elif level == "==":
                # Only a fresh top-level section resumes output, so sub-headings
                # nested under a noise section (e.g. ``=== Lesser known
                # collections ===`` under ``== Publications ==``) stay skipped.
                skipping = False
            continue
        if skipping or not line:
            continue
        if _is_noise_line(line):
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            sentence = sentence.strip()
            if sentence and not _is_noise_line(sentence):
                facts.append(sentence)
    return facts


class PromptIngestor(Ingestor):
    """Distill via a single prompted LLM call (or cleaned passthrough when no LLM)."""

    def __init__(self, graph: KnowledgeGraph, llm: LLM | None = None) -> None:
        super().__init__(graph)
        self.llm = llm

    def synthesis(
        self, raw_input: str, *, source: str | None = None, atomic: bool = False
    ) -> list[Insight]:
        # Atomic lane (shaped-fact writes, e.g. ``add_insight``): the input is an
        # already-distilled, self-contained fact — keep it WHOLE as a single
        # insight. Skip BOTH the table linearizer and the sentence splitter so a
        # multi-sentence requirement (with its "Acceptance: ..." clause) stays one
        # fact rather than fragmenting one-per-sentence. Dedup + contradiction
        # surfacing still run downstream in ``graph.write``.
        if atomic:
            return [Insight(raw_text=raw_input.strip())]
        # Split raw input into discrete ideas only — no graph read. Reconciling
        # with existing knowledge happens later in ``graph.write``.
        #
        # Table branch (path-independent, checked first): detected tabular/templated
        # input is linearized deterministically rather than handed to the prose
        # SPLIT_PROMPT / sentence splitter, which collapse or mangle rows sharing a
        # sentence shape (loss point A). This is the primary fix, not a prompt tweak —
        # the reference research is blunt that prompt interventions don't fix
        # structural extraction. Each row is flagged ``tabular`` so the deduper's
        # slot-guard engages downstream (loss point B).
        table_facts = linearize_table(raw_input)
        if table_facts:
            return [Insight(raw_text=fact, tabular=True) for fact in table_facts]
        if self.llm is None:
            # No model to distill with: clean obvious document noise and segment
            # into atomic sentences so we don't dump raw chunks into the graph.
            # The table branch above already ruled out tabular input, so segment
            # the prose directly (skips a redundant second linearize_table pass).
            return [Insight(raw_text=fact) for fact in _segment_prose(raw_input)]
        # The SOURCE line gives the model document context so it can resolve
        # in-document references (e.g. a bare "line 12") into self-contained facts.
        context = f"SOURCE: {source}\n\n" if source else ""
        prompt = f"{SPLIT_PROMPT}\n\n{context}INPUT:\n{raw_input}"
        text = self.llm(prompt)
        return [Insight(raw_text=line.strip()) for line in text.splitlines() if line.strip()]
