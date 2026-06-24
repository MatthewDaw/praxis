"""Generate the tax-return eval set for Matt.

These are FULL-PIPELINE cases (component: null), built the same way as the
application evals (matt/applications/_generate.py):

  - Raw source documents are ingested *raw* through the ingestor
    (``seeded_insight.via_ingestor``) — NOT hand-curated facts:
      1. ``sources/form_1040_instructions.txt`` — SHARED line-by-line rules,
         TY2025 standard deductions, and single/MFJ/HOH brackets.
      2. per-scenario W-2(s) and intake Q&A, written into ``sources/<slug>/`` for
         provenance and stamped into the scenario's ``case.yaml``.
    The ingestor distils all of it into the knowledge graph.
  - A GENERIC ``seed_prompt`` ("fill out the return") drives the boxed agent. It
    names no numbers — the agent must pull the W-2 amounts, the filing status
    from the Q&A, and the rules from the instructions out of the ingested
    knowledge, then do the arithmetic. So the prompt "just works".
  - Deterministic checks assert the computed return lands the correct figures; a
    rubric grades grounding / arithmetic / completeness / honesty.

Plus one ``component: graph_reader`` retrieval case (``retrieval_recall``) that
asserts the reader actually surfaces the facts an agent needs to fill the return
(recall), grading the retrieval set itself rather than only the answer.

This is the PROVING eval for "an external agent can offload its knowledge layer
to Praxis": raw tax docs in -> ingest/distil -> retrieve -> a correct 1040 out.

All arithmetic is computed here (single source of truth) so the deterministic
checks and the rubric can never drift from the canonical figures. Edit this
script (or the shared instructions) and re-run:

    uv run python knowledge/evals/cases/matt/tax_return/_generate.py
"""

from __future__ import annotations

from pathlib import Path

import yaml

HERE = Path(__file__).parent
SRC = HERE / "sources"

INSTRUCTIONS = (SRC / "form_1040_instructions.txt").read_text(encoding="utf-8")

# --- TY2025 tax facts (mirror sources/form_1040_instructions.txt exactly) ----

STANDARD_DEDUCTION = {
    "single": 15_750,
    "mfj": 31_500,
    "hoh": 23_625,
    "mfs": 15_750,
}

# (marginal rate, upper bound of the bracket); last bracket's bound is a ceiling
# above any income in this eval set.
BRACKETS = {
    "single": [(0.10, 11_925), (0.12, 48_475), (0.22, 103_350), (0.24, 197_300)],
    "mfj": [(0.10, 23_850), (0.12, 96_950), (0.22, 206_700), (0.24, 394_600)],
    "hoh": [(0.10, 17_000), (0.12, 64_850), (0.22, 103_350), (0.24, 197_300)],
    "mfs": [(0.10, 11_925), (0.12, 48_475), (0.22, 103_350), (0.24, 197_300)],
}

STATUS_LABEL = {
    "single": "Single",
    "mfj": "Married filing jointly",
    "hoh": "Head of household",
    "mfs": "Married filing separately",
}


def compute_tax(taxable: int, status: str) -> int:
    """Marginal tax on ``taxable`` for ``status``, rounded half-up to whole dollars."""
    tax = 0.0
    lower = 0
    for rate, upper in BRACKETS[status]:
        if taxable > lower:
            tax += rate * (min(taxable, upper) - lower)
            lower = upper
        else:
            break
    return int(tax + 0.5)


def comma_optional(amount: int) -> str:
    """Regex fragment matching the amount with optional thousands commas and $."""
    return r"\$?" + f"{amount:,}".replace(",", "[,]?")


# --- W-2 and intake Q&A source templates -------------------------------------


def w2_text(emp: dict) -> str:
    ss = round(emp["wages"] * 0.062, 2)
    medi = round(emp["wages"] * 0.0145, 2)
    return f"""Form W-2 — Wage and Tax Statement (Tax Year 2025)

Employee: {emp['name']}
Employee SSN: XXX-XX-{emp['ssn_last4']}

Employer: {emp['employer']}
Employer EIN: {emp['ein']}

Box 1  — Wages, tips, other compensation .......... {emp['wages']:,.2f}
Box 2  — Federal income tax withheld .............. {emp['withholding']:,.2f}
Box 3  — Social Security wages .................... {emp['wages']:,.2f}
Box 4  — Social Security tax withheld ............. {ss:,.2f}
Box 5  — Medicare wages and tips ................. {emp['wages']:,.2f}
Box 6  — Medicare tax withheld .................... {medi:,.2f}
Box 13 — Retirement plan: not checked
Box 15 — State: {emp['state']}
"""


def qa_text(scn: dict) -> str:
    names = " and ".join(e["name"] for e in scn["employees"])
    only_w2 = (
        "Yes, this is our only income; each spouse has one W-2 and nothing else."
        if scn["status"] == "mfj"
        else "Yes, that W-2 is my only income for the year."
    )
    return f"""Taxpayer intake questionnaire — answers submitted by {names} (Tax Year 2025)

These are the taxpayer's own answers to the preparer's intake questions; they
supply the details that are not printed on the W-2(s).

Q: What is your filing status for 2025?
A: {STATUS_LABEL[scn['status']]}.

Q: Do you have any dependents?
A: No dependents.

Q: Did you have any income besides your W-2 wages — interest, dividends,
   self-employment, capital gains, or anything else?
A: {only_w2}

Q: Will you take the standard deduction or itemize?
A: Take the standard deduction; we do not have enough deductible expenses to itemize.

Q: Did you make any estimated tax payments, or are you claiming any credits?
A: No estimated payments and no credits apply to us this year. The only federal
   tax paid was the amount withheld from our paychecks (Box 2 of the W-2s).
"""


# --- Scenario table ----------------------------------------------------------
# Each scenario lists its filing status and W-2(s); figures are computed below.

SCENARIOS = [
    {
        "slug": "single_w2",
        "status": "single",
        # Mirrors the hackathon taxpayer profile: single, one W-2, ~$40k/yr.
        "employees": [
            {
                "name": "Jordan A. Rivera",
                "ssn_last4": "4821",
                "employer": "Lakeside Logistics LLC",
                "ein": "36-7741920",
                "state": "IL",
                "wages": 40_000,
                "withholding": 3_200,
            }
        ],
    },
    {
        "slug": "mfj_two_w2",
        "status": "mfj",
        # Married filing jointly: two W-2s must be aggregated.
        "employees": [
            {
                "name": "Dana R. Okafor",
                "ssn_last4": "1190",
                "employer": "Riverbend Health Partners",
                "ein": "45-2210087",
                "state": "OH",
                "wages": 40_000,
                "withholding": 3_200,
            },
            {
                "name": "Samuel T. Okafor",
                "ssn_last4": "7732",
                "employer": "Greenfield School District",
                "ein": "31-6041550",
                "state": "OH",
                "wages": 35_000,
                "withholding": 2_600,
            },
        ],
    },
    {
        "slug": "single_owes",
        "status": "single",
        # Single, under-withheld: must land an amount OWED, not a refund.
        "employees": [
            {
                "name": "Priya N. Shah",
                "ssn_last4": "5567",
                "employer": "Northstar Analytics Inc.",
                "ein": "27-8890123",
                "state": "CA",
                "wages": 90_000,
                "withholding": 9_000,
            }
        ],
    },
]

SEED_PROMPT = (
    "You are a tax-preparation agent. Your knowledge base has been seeded with "
    "everything needed to prepare this taxpayer's U.S. federal income tax return: "
    "the Form 1040 filling-out instructions, the W-2(s), and the taxpayer's "
    "answers to an intake questionnaire. Using ONLY that ingested knowledge, fill "
    "out the federal income tax return. Work the Form 1040 lines in order, show "
    "the figure for each line, and do the arithmetic (taxable income and tax) "
    "yourself from the rules and amounts you were given — do not invent numbers "
    "that are not supported by the ingested knowledge, and do not ask the user for "
    "information that is already there. End with the bottom line: the taxpayer's "
    "refund or the amount they owe. Write the completed return to a file named "
    "answer.md and create no other files."
)


def build_scenario(scn: dict) -> dict:
    status = scn["status"]
    total_wages = sum(e["wages"] for e in scn["employees"])
    total_withholding = sum(e["withholding"] for e in scn["employees"])
    std = STANDARD_DEDUCTION[status]
    taxable = max(0, total_wages - std)
    tax = compute_tax(taxable, status)
    if total_withholding >= tax:
        refund, owed = total_withholding - tax, 0
    else:
        refund, owed = 0, tax - total_withholding

    case_id = f"matt_tax_return_{scn['slug']}"

    # Write per-scenario sources for provenance, and collect the ingested text.
    scn_dir = SRC / scn["slug"]
    scn_dir.mkdir(parents=True, exist_ok=True)
    sources = [INSTRUCTIONS]
    for i, emp in enumerate(scn["employees"], start=1):
        text = w2_text(emp)
        suffix = f"_{i}" if len(scn["employees"]) > 1 else ""
        (scn_dir / f"w2{suffix}.txt").write_text(text, encoding="utf-8")
        sources.append(text)
    qa = qa_text(scn)
    (scn_dir / "qa_submissions.txt").write_text(qa, encoding="utf-8")
    sources.append(qa)

    bottom = (
        ("states_refund_amount", comma_optional(refund))
        if owed == 0
        else ("states_owed_amount", comma_optional(owed))
    )
    checks = [
        NONEMPTY,
        regex_check("states_total_income", comma_optional(total_wages)),
        regex_check("states_standard_deduction", comma_optional(std)),
        regex_check("states_taxable_income", comma_optional(taxable)),
        regex_check("states_computed_tax", comma_optional(tax)),
        regex_check("states_withholding", comma_optional(total_withholding)),
        regex_check(*bottom),
        regex_check(
            "calls_bottom_line", r"(?i)refund" if owed == 0 else r"(?i)\bowe"
        ),
        # No credits apply in any scenario — guard against fabricated ones.
        absent_check(
            "no_fabricated_credit",
            r"(?i)(child tax credit|earned income credit|education credit|"
            r"american opportunity credit)",
        ),
    ]

    if owed == 0:
        bottom_desc = f"refund = {total_withholding:,} - {tax:,} = {refund:,}"
        bottom_line = f"a REFUND of ${refund:,}, not an amount owed"
    else:
        bottom_desc = f"amount owed = {tax:,} - {total_withholding:,} = {owed:,}"
        bottom_line = f"an AMOUNT OWED of ${owed:,}, not a refund"

    rubric = {
        "id": f"{case_id}_v1",
        "items": [
            {
                "id": "grounded",
                "criterion": (
                    "Every figure on the return traces to the ingested sources: "
                    "wages and withholding from the W-2(s), filing status / "
                    "standard-deduction choice from the intake Q&A, deduction "
                    "amount and brackets from the instructions. No invented income, "
                    "credits, or amounts."
                ),
                "weight": 2.0,
            },
            {
                "id": "correct_arithmetic",
                "criterion": (
                    f"The math is right for a {STATUS_LABEL[status]} filer: total "
                    f"income {total_wages:,}; standard deduction {std:,}; taxable "
                    f"income {total_wages:,} - {std:,} = {taxable:,}; tax on the "
                    f"{STATUS_LABEL[status]} brackets = {tax:,}; {bottom_desc}. The "
                    f"bottom line is {bottom_line}."
                ),
                "weight": 2.5,
            },
            {
                "id": "complete",
                "criterion": (
                    "The return covers the full chain in order — filing status, "
                    "total income, AGI, standard deduction, taxable income, tax, "
                    "federal tax withheld, and the final refund/amount owed — rather "
                    "than jumping straight to an answer."
                ),
                "weight": 1.5,
            },
            {
                "id": "no_questions_back",
                "criterion": (
                    "The agent fills out the return directly from the seeded "
                    "knowledge instead of stopping to ask the user for details "
                    "(filing status, deduction choice, other income) that the intake "
                    "Q&A already supplies."
                ),
                "weight": 1.0,
            },
        ],
    }

    case = {
        "id": case_id,
        # Full real pipeline (see matt/applications/_generate.py for the rationale
        # on each knob): vector substrate, committed/cached vectors, real ingest
        # distillation, facts land active+retrievable, retrieving reader with the
        # volume cap off so facts from all three docs are ranked together.
        "substrate": "vector",
        "embedder": "cached",
        "ingest_model": "openai/gpt-4o-mini",
        "ingest_state": "active",
        "reader": "retrieving",
        "reader_top_k": 0,
        "seed_prompt": SEED_PROMPT,
        "target_commit": "0" * 40,
        "needs": ["file_io"],
        "seeded_insight": {"via_ingestor": sources},
        "deterministic_checks": checks,
        "rubric": rubric,
    }
    return {"case": case, "dir": HERE / scn["slug"]}


def build_recall_case() -> dict:
    """A graph_reader retrieval case: prove the reader surfaces what the agent needs.

    Reuses the single-filer scenario's seeded docs and asserts the salient facts
    (wages, filing status, standard deduction) come back in the reader output for
    a fill-the-return query. This grades the *retrieval set*, not the answer.
    """
    scn = SCENARIOS[0]  # single_w2
    emp = scn["employees"][0]
    sources = [INSTRUCTIONS, w2_text(emp), qa_text(scn)]
    case = {
        "id": "matt_tax_return_retrieval_recall",
        "component": "graph_reader",
        "substrate": "vector",
        "embedder": "cached",
        "ingest_model": "openai/gpt-4o-mini",
        "ingest_state": "active",
        "reader": "retrieving",
        "reader_top_k": 0,
        "seed_prompt": (
            "What wages, federal tax withheld, filing status, and standard "
            "deduction apply to this taxpayer's 2025 Form 1040, and how is the tax "
            "computed?"
        ),
        "seeded_insight": {"via_ingestor": sources},
        "deterministic_checks": [
            regex_check("recall_wages", comma_optional(emp["wages"])),
            regex_check("recall_withholding", comma_optional(emp["withholding"])),
            regex_check("recall_standard_deduction", comma_optional(15_750)),
            regex_check("recall_filing_status", r"(?i)single"),
            regex_check("recall_deduction_concept", r"(?i)standard deduction"),
        ],
    }
    return {"case": case, "dir": HERE / "retrieval_recall"}


def regex_check(name: str, pattern: str) -> dict:
    return {
        "name": name,
        "ref": "knowledge.evals.deterministic_checks.text:regex_matches",
        "params": {"pattern": pattern},
    }


def absent_check(name: str, pattern: str) -> dict:
    return {
        "name": name,
        "ref": "knowledge.evals.deterministic_checks.text:regex_absent",
        "params": {"pattern": pattern},
    }


NONEMPTY = {
    "name": "produced_answer",
    "ref": "knowledge.evals.deterministic_checks.builds:output_nonempty",
    "params": {},
}

HEADER = (
    "# GENERATED by _generate.py — edit that script (or the sources/), not this file.\n"
    "# Tax-return eval: Form 1040 instructions + W-2(s) + intake Q&A are ingested raw;\n"
    "# a generic 'fill out the return' prompt drives the agent to write answer.md.\n"
)


def main() -> None:
    written = 0
    for scn in SCENARIOS:
        built = build_scenario(scn)
        out = built["dir"] / "case.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            HEADER
            + yaml.safe_dump(
                built["case"], sort_keys=False, allow_unicode=True, width=4096
            ),
            encoding="utf-8",
        )
        written += 1

    recall = build_recall_case()
    out = recall["dir"] / "case.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    recall_header = (
        "# GENERATED by _generate.py — edit that script, not this file.\n"
        "# Retrieval recall case (graph_reader): asserts the reader surfaces the\n"
        "# facts an agent needs to fill the 1040 (recall), grading the retrieval set.\n"
    )
    out.write_text(
        recall_header
        + yaml.safe_dump(recall["case"], sort_keys=False, allow_unicode=True, width=4096),
        encoding="utf-8",
    )
    written += 1

    print(f"wrote {written} case(s)")


if __name__ == "__main__":
    main()
