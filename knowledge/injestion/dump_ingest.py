"""Document-scoped ("dump") ingestion with slot-granular dedup + conflict resolution.

The recurring failure on tabular data (tax brackets, per-status deductions, W-2
boxes) is *slot coarsening*: a judge that treats "MFJ bracket" as one slot
(ignoring the income range) or "standard deduction" as one slot (ignoring filing
status) sees every table row as "same subject, different value" and false-flags
them as mutual contradictions. Both free-text contradiction judgment and the
coarse claim extractor hit this.

The fix is to make conflict detection robust by construction, in two reliable
pieces instead of one unreliable one:

  1. **Granular claims from the single distillation call.** Each self-contained
     fact comes with a structured claim ``(subject, attribute, value)`` whose
     SUBJECT includes every discriminating qualifier — filing status, income
     range, line/box number, year. So two bracket rows have *different subjects*.
  2. **A "same specific subject?" judgment** (one batched LLM call over the
     overlaps), which is tractable precisely because the subjects are granular —
     "MFJ bracket for $23,850–$96,950" and "MFJ bracket for $96,950–$206,700" are
     plainly different subjects, while "standard deduction for Single filers"
     stated two ways is plainly the same.
  3. **A structural value comparison** (no LLM): for facts that DO share a slot,
     equal value -> duplicate (merge, keep the richer phrasing); different value
     -> genuine conflict (reject the loser, ``contradicted_by`` edge, newest
     wins). Numbers compare numerically; text by normalized equality.

The claim is persisted on each fact's ``meta`` so later documents can resolve
their overlaps against it. Cost: ~1 distillation call per document plus an
occasional batched same-slot call — and table rows are never false-flagged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from knowledge.injestion.injestor_variants.prompt_injestor import SPLIT_PROMPT
from knowledge.llm.llm_def import ChatMessage
from knowledge.llm.parent_llm import Llm

_DISTILL_PROMPT = (
    SPLIT_PROMPT
    + "\n\nFor EACH fact also return a structured claim with three fields:\n"
    "- subject: the specific thing the fact is about, including EVERY qualifier "
    "that distinguishes it from related facts — filing status, income range, form "
    "line or box number, tax year, etc. Two facts must share a subject ONLY if "
    "they are about the exact same specific thing. CRITICAL: when a fact is ONE ROW "
    "of a table or schedule (e.g. a single tax-bracket row, a per-filing-status "
    "amount), the subject MUST embed that row's distinguishing key — the exact "
    "income range for a bracket, the filing status for a deduction — or different "
    "rows will be wrongly treated as the same thing. Every bracket row therefore "
    "gets a DIFFERENT subject. (Good: \"TY2025 Head of household ordinary-income tax "
    "bracket for income $250,500-$626,350\". Bad: \"Head of household tax bracket\" "
    "or \"tax bracket\" — these collapse distinct rows.)\n"
    "- attribute: the property being asserted about the subject (e.g. \"rate\", "
    "\"amount\", \"contents\", \"line number\").\n"
    "- value: the value of that attribute (e.g. \"12%\", \"$15,750\")."
)
_DISTILL_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "distillation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "subject": {"type": "string"},
                            "attribute": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["text", "subject", "attribute", "value"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["facts"],
            "additionalProperties": False,
        },
    },
}

_SLOT_PROMPT = (
    "Each numbered item shows two claims, A (new) and B (existing), each as "
    "subject | attribute. Two claims share a SLOT only if A and B describe the "
    "EXACT SAME specific subject AND the same attribute — identical entity "
    "including every qualifier (filing status, income range, line/box number, "
    "year). Different rows of a table (different income ranges, different filing "
    "statuses, different line numbers) DO NOT share a slot even when the topic "
    "matches. Return the indices of the pairs that share a slot."
)
_SLOT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "same_slot",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"same_slot": {"type": "array", "items": {"type": "integer"}}},
            "required": ["same_slot"],
            "additionalProperties": False,
        },
    },
}

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# Tokens too generic to signal that two subjects are the same specific thing.
_STOP = {"the", "a", "an", "of", "for", "on", "in", "to", "is", "are", "and",
         "or", "that", "this", "ty2025", "tax", "year", "form", "1040"}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _content_tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOP}


def _subject_overlap(s1: str, s2: str) -> float:
    """Jaccard of content tokens — how much two subjects actually share."""
    t1, t2 = _content_tokens(s1), _content_tokens(s2)
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


# Generic identifiers that don't distinguish a subject (the form itself, the year).
_GENERIC_IDS = {"1040", "2025", "ty2025"}


def _id_tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"\d+[a-z]?", (s or "").lower())} - _GENERIC_IDS


def _distinct_identifiers(s1: str, s2: str) -> bool:
    """True when both subjects carry identifiers (line/box numbers, ranges) and
    share none — e.g. "line 12" vs "line 25a". Different identifiers => different
    specific subject, so the pair can never be a conflict no matter what the LLM
    or generic word-overlap says."""
    i1, i2 = _id_tokens(s1), _id_tokens(s2)
    return bool(i1) and bool(i2) and not (i1 & i2)


# A slot match is accepted only when the subjects genuinely overlap, not just
# when the LLM said so — guards against it conflating e.g. "W-2 box 2" and
# "standard deduction" because both are "entered on a Form 1040 line".
_SUBJECT_OVERLAP_MIN = 0.2


def _same_value(v1: str, v2: str) -> bool:
    """Equal value? Numbers compare numerically; otherwise normalized text.

    Generic identifiers (the form number 1040, the year 2025) are stripped first,
    so "Form 1040 line 12" and "line 12" compare on 12, not on 1040 vs 12.
    """
    n1 = [n for n in _NUM.findall(v1 or "") if n.replace(",", "") not in _GENERIC_IDS]
    n2 = [n for n in _NUM.findall(v2 or "") if n.replace(",", "") not in _GENERIC_IDS]
    if n1 and n2:
        try:
            return abs(float(n1[0].replace(",", "")) - float(n2[0].replace(",", ""))) < 1e-9
        except ValueError:
            pass
    return _norm(v1) == _norm(v2)


def _distill(llm: Llm, raw_input: str, source: str | None) -> list[dict[str, str]]:
    """One call: self-contained facts, each with a granular (subject, attribute, value) claim."""
    context = f"SOURCE: {source}\n\n" if source else ""
    content = f"{_DISTILL_PROMPT}\n\n{context}INPUT:\n{raw_input}"
    raw = llm.complete([ChatMessage(role="user", content=content)], response_format=_DISTILL_SCHEMA)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return [{"text": ln.strip(), "subject": ln.strip(), "attribute": "", "value": ""}
                for ln in str(raw).splitlines() if ln.strip()]
    out = []
    for f in data.get("facts", []):
        text = str(f.get("text", "")).strip()
        if text:
            out.append({
                "text": text,
                "subject": str(f.get("subject", "")).strip(),
                "attribute": str(f.get("attribute", "")).strip(),
                "value": str(f.get("value", "")).strip(),
            })
    return out


_FACT_PROMPT = (
    "Each numbered item pairs a NEW fact with an EXISTING fact. Return the indices "
    "of pairs that state the SAME fact: the same information about the same thing, "
    "merely reworded or one a more detailed refinement, asserting the SAME value. "
    "Pairs that assert DIFFERENT values, or are about different things, are NOT the "
    "same fact. Return an empty list when unsure."
)
_FACT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "same_fact",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"same": {"type": "array", "items": {"type": "integer"}}},
            "required": ["same"],
            "additionalProperties": False,
        },
    },
}


def _same_fact(llm: Llm, pairs: list[tuple[str, str]]) -> list[int]:
    """One batched call: which (new_text, existing_text) pairs are the same fact.

    Permissive about phrasing (different subject/attribute wording is fine) but
    strict on value — so it dedups restatements without merging genuine conflicts.
    """
    if not pairs:
        return []
    lines = [f"{i}. NEW: {a}\n   EXISTING: {b}" for i, (a, b) in enumerate(pairs)]
    raw = llm.complete(
        [ChatMessage(role="user", content=_FACT_PROMPT + "\n\n" + "\n".join(lines))],
        response_format=_FACT_SCHEMA,
    )
    try:
        idxs = json.loads(raw).get("same", [])
    except (TypeError, ValueError):
        return []
    return [i for i in idxs if isinstance(i, int) and 0 <= i < len(pairs)]


def _same_slot(llm: Llm, pairs: list[tuple[dict, dict]]) -> list[int]:
    """One batched call: which (A_new, B_existing) claim pairs share a slot."""
    if not pairs:
        return []
    lines = [
        f"{i}. A: {a['subject']} | {a['attribute']}\n   B: {b['subject']} | {b['attribute']}"
        for i, (a, b) in enumerate(pairs)
    ]
    content = _SLOT_PROMPT + "\n\n" + "\n".join(lines)
    raw = llm.complete([ChatMessage(role="user", content=content)], response_format=_SLOT_SCHEMA)
    try:
        idxs = json.loads(raw).get("same_slot", [])
    except (TypeError, ValueError):
        return []
    return [i for i in idxs if isinstance(i, int) and 0 <= i < len(pairs)]


def ingest_dump(
    graph: Any,
    llm: Llm,
    raw_input: str,
    *,
    state: str = "active",
    source: str | None = None,
    external_top_k: int = 5,
    on_conflict: str = "auto_resolve",
) -> dict[str, Any]:
    """Ingest one document: distill (granular claims), write, then resolve overlaps —
    same-slot+same-value -> merge (dedup); same-slot+different-value -> conflict.

    ``on_conflict`` controls what a same-slot, different-value clash does:

    * ``"auto_resolve"`` (default) — reject the losing fact and link it to the
      winner with a ``contradicted_by`` edge (newest approved truth wins).
    * ``"surface"`` — keep BOTH facts and record a *pending* ``contradiction`` edge
      so the clash shows up in ``GET /contradictions`` for human adjudication. The
      later (losing) side is demoted to ``proposed`` so the pair is never both
      ``active`` (FR-005); neither side is rejected.

    Returns a per-stage summary (``surfaced`` counts pending contradictions raised).
    """
    raw_input = (raw_input or "").strip()
    if not raw_input:
        return {"facts": 0, "merged": 0, "conflicts": 0, "surfaced": 0, "rejected": []}
    surface = on_conflict == "surface"

    distilled = _distill(llm, raw_input, source)
    facts = [d["text"] for d in distilled]
    claims = {i: {"subject": d["subject"], "attribute": d["attribute"], "value": d["value"]}
              for i, d in enumerate(distilled)}

    # Established facts (pre-dump) + their stored claims, for external resolution.
    established = graph.all_facts(state=None)
    est_claim = {f.id: (f.meta or {}).get("claim") for f in established}
    established_ids = set(est_claim)

    new_ids: list[str | None] = [
        graph.write(d["text"], state=state, source=source, meta={"claim": claims[i]})
        for i, d in enumerate(distilled)
    ]

    removed: set[str] = set()
    merged = 0
    rejected: list[str] = []

    def _live(fid):
        return fid and fid not in removed

    def _merge(keep_id, keep_text, drop_id, drop_text):
        nonlocal merged
        if len(drop_text) > len(keep_text):
            graph.update_fact(keep_id, text=drop_text)
        graph.delete_fact(drop_id)
        removed.add(drop_id)
        merged += 1

    surfaced = 0

    def _conflict(win_id, lose_id):
        graph.set_state(lose_id, "rejected")
        graph.add_edge(win_id, lose_id, "contradicted_by")
        rejected.append(lose_id)
        removed.add(lose_id)

    def _surface(active_id, demote_id):
        # Surface mode: keep both facts and record a *pending* contradiction for
        # human adjudication. The active incumbent keeps its place; the other side
        # is demoted to proposed so the pair is never both active (FR-005). Nothing
        # is rejected. ``demote_id`` is dropped from further in-dump pairing only.
        nonlocal surfaced
        graph.add_edge(active_id, demote_id, "contradiction")
        if state == "active":
            graph.set_state(demote_id, "proposed")
        surfaced += 1
        removed.add(demote_id)

    # 1) Internal (within-dump) same-slot pairs — phrasing is consistent within one
    #    distillation call, so match structurally on normalized (subject, attribute).
    by_slot: dict[tuple[str, str], list[int]] = {}
    for i in range(len(facts)):
        key = (_norm(claims[i]["subject"]), _norm(claims[i]["attribute"]))
        if not key[0]:
            continue
        by_slot.setdefault(key, []).append(i)
    for idxs in by_slot.values():
        for j in idxs[1:]:
            a = idxs[0]
            if not (_live(new_ids[a]) and _live(new_ids[j]) and new_ids[a] != new_ids[j]):
                continue
            if _same_value(claims[a]["value"], claims[j]["value"]):
                _merge(new_ids[a], facts[a], new_ids[j], facts[j])
            elif surface:
                _surface(new_ids[a], new_ids[j])  # keep earlier active, demote later
            else:
                _conflict(new_ids[a], new_ids[j])  # keep earlier, reject later

    # 2) External overlaps: recall established facts, build candidate pairs.
    cands: list[tuple[str, int, str, str, dict]] = []  # (new_fid,new_idx,est_id,est_text,est_claim)
    seen: set[tuple[str, str]] = set()
    for i, fid in enumerate(new_ids):
        if not _live(fid) or fid in established_ids:
            continue
        for hit in graph.search(facts[i], top_k=external_top_k, state=None):
            ec = est_claim.get(hit.fact.id)
            key = (fid, hit.fact.id)
            if hit.fact.id in established_ids and hit.fact.id not in removed and ec and key not in seen:
                seen.add(key)
                cands.append((fid, i, hit.fact.id, hit.fact.text, ec))

    # 2a) DEDUP: which candidates state the SAME fact (same meaning + value),
    # regardless of how subject/attribute are phrased. Catches cross-document
    # restatements that share no literal slot ("box 2 -> line 25a" vs
    # "line 25a = withheld tax").
    for k in _same_fact(llm, [(facts[ni], et) for (_f, ni, _e, et, _c) in cands]):
        nf, ni, ei, et, _c = cands[k]
        if _live(nf) and ei not in removed:
            _merge(ei, et, nf, facts[ni])  # keep the established incumbent

    # 2b) CONFLICT: among survivors, a genuine clash needs the SAME attribute,
    # genuinely overlapping subjects (so the slot judge can't conflate facts that
    # merely share a generic attribute like "entered on a Form 1040 line"), the
    # same specific slot (LLM), and a DIFFERENT value.
    slot = [
        c for c in cands
        if _live(c[0]) and c[2] not in removed
        and _norm(claims[c[1]]["attribute"]) == _norm(c[4].get("attribute", ""))
        and _subject_overlap(claims[c[1]]["subject"], c[4].get("subject", "")) >= _SUBJECT_OVERLAP_MIN
        and not _distinct_identifiers(claims[c[1]]["subject"], c[4].get("subject", ""))
    ]
    conflicts = 0
    for j in _same_slot(llm, [(claims[c[1]], c[4]) for c in slot]):
        nf, ni, ei, et, ec = slot[j]
        if not _live(nf) or ei in removed:
            continue
        if _same_value(claims[ni]["value"], ec.get("value", "")):
            _merge(ei, et, nf, facts[ni])  # same slot + same value the dedup pass missed
        elif surface:
            _surface(ei, nf)  # incumbent stays active; demote the newcomer, keep both
        else:
            _conflict(nf, ei)  # newest wins: reject the established fact
            conflicts += 1

    return {
        "facts": len(facts),
        "merged": merged,
        "conflicts": conflicts,
        "surfaced": surfaced,
        "rejected": rejected,
    }
