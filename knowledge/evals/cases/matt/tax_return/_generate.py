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
        # Disable the rel_ratio shape filter (mirrors build_recall_case). The agent
        # is told it has "everything needed" — Form 1040 rules, the W-2(s), AND the
        # intake answers — but a non-default fact like the filing status ("Married
        # filing jointly") scores under 60% of the top hit against the generic
        # seed prompt, so the default rel_ratio=0.60 drops it before the agent ever
        # sees it, silently forcing the Single default. The abs_floor=0.30 existence
        # gate (kept at default) remains the relevance guard.
        "reader_rel_ratio": 0.0,
        # Also drop the abs_floor existence gate: the taxpayer's intake answer
        # (e.g. "filing status: Married filing jointly") scores below 0.30 against
        # the generic seed prompt, so abs_floor=0.30 still buries it and the agent
        # falls back to the Single default. The prompt promises the agent has
        # "everything needed", so surface the full seeded set and let the agent rank.
        "reader_abs_floor": 0.0,
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


# --- Ruleset-distillation integrity case -------------------------------------
# Ingests the EXACT harness corpus the live graph was seeded from — the 26
# `app/rules.py` RULE_DOCUMENTS (verbatim copy below) — through the full
# live-like write policy, then asserts the reader surfaces every salient fact an
# agent needs to file a 1040 across all four filing statuses. This is the real
# reproduction of the audit: distilling the whole rule set creates dense
# cross-status near-duplicate pressure (Single==MFS==$15,750 standard deductions;
# overlapping bracket ranges across Single/MFS/HoH), which is what makes the
# status-blind dedup/merge drop a row.
#
# The defect this guards: cross-status range collisions silently collapse a
# filing-status fact. Observed live BEFORE the H6 slot-guard fix: Single 12% /
# 24% / 35% rejected. AFTER the fix + a clean re-seed: those three returned, but
# Single 22% ($48,475-$103,350, the range it shares with MFS 22%) is now silently
# merged into the MFS twin and absent from the Single ladder. So the checks below
# are driven off the FULL ladders for all four statuses — they catch whichever
# row the collision drops, not just yesterday's victims — plus the standard
# deductions, the marginal/sum rule, the W-2 box mappings, and the Form 1040 line
# flow, so the whole retrieval set is guarded, not only the brackets.

# Verbatim copy of app/rules.py RULE_DOCUMENTS (the harness ingest source). Kept
# inline so this praxis-repo eval stays self-contained; if the harness rule text
# changes, re-copy it here and re-run _generate.py + embed_cache --add.
RULESET_DOCS = [
    "TY2025 Form 1040 standard deduction by filing status: Single $15,750; "
    "Married filing jointly $31,500; Married filing separately $15,750; "
    "Head of household $23,625. Enter on Form 1040 line 12.",
    "TY2025 standard deduction for a Single filer is $15,750 (Form 1040 line 12).",
    "TY2025 standard deduction for Married filing jointly is $31,500 (Form 1040 line 12).",
    "TY2025 standard deduction for Married filing separately is $15,750 (Form 1040 line 12).",
    "TY2025 standard deduction for Head of household is $23,625 (Form 1040 line 12).",
    "A taxpayer who is age 65 or older OR blind gets an additional standard "
    "deduction on top of the basic amount; a taxpayer who is both gets it twice. "
    "Most filers under 65 and not blind use only the basic standard deduction.",
    "On Form 1040 line 12 a taxpayer claims the LARGER of the standard deduction "
    "or total itemized deductions (Schedule A). Most W-2 wage earners take the "
    "standard deduction because their itemized deductions are smaller.",
    "TY2025 ordinary income tax brackets, Single: 10% up to $11,925; "
    "12% $11,925-$48,475; 22% $48,475-$103,350; 24% $103,350-$197,300; "
    "32% $197,300-$250,525; 35% $250,525-$626,350; 37% above $626,350.",
    "TY2025 ordinary income tax brackets, Married filing jointly: 10% up to "
    "$23,850; 12% $23,850-$96,950; 22% $96,950-$206,700; 24% "
    "$206,700-$394,600; 32% $394,600-$501,050; 35% $501,050-$751,600; "
    "37% above $751,600.",
    "TY2025 ordinary income tax brackets, Married filing separately: 10% up to "
    "$11,925; 12% $11,925-$48,475; 22% $48,475-$103,350; 24% $103,350-$197,300; "
    "32% $197,300-$250,525; 35% $250,525-$375,800; 37% above $375,800. "
    "(MFS thresholds match Single except in the top two brackets.)",
    "TY2025 ordinary income tax brackets, Head of household: 10% up to "
    "$17,000; 12% $17,000-$64,850; 22% $64,850-$103,350; 24% "
    "$103,350-$197,300; 32% $197,300-$250,500; 35% $250,500-$626,350; "
    "37% above $626,350.",
    "Federal income tax brackets are marginal: each rate applies only to the "
    "portion of taxable income that falls within that bracket's range, not to "
    "the entire income. Sum the tax from each bracket to get total tax.",
    "Single filing status applies to a taxpayer who is unmarried, divorced, or "
    "legally separated on the last day of the tax year and does not qualify for "
    "head of household.",
    "Married couples may file jointly (MFJ), combining both spouses' income on "
    "one return, or separately (MFS) on two returns. MFJ usually yields a lower "
    "combined tax than MFS.",
    "Head of household applies to an unmarried taxpayer who paid more than half "
    "the cost of keeping up a home for a qualifying person (such as a dependent "
    "child) for more than half the year. It gives a larger standard deduction "
    "and wider brackets than Single.",
    "A taxpayer under 65 generally must file a 2025 federal return if their gross "
    "income is at least the standard deduction for their filing status (e.g. "
    "$15,750 for Single). Filing is also worthwhile to claim a refund of withheld tax.",
    "W-2 box 1 reports taxable wages, tips, and other compensation. It can be lower "
    "than boxes 3 and 5 when the employee made pre-tax contributions such as a "
    "401(k). Use box 1 for Form 1040 line 1a, not box 3 or 5.",
    "W-2 box 2 reports federal income tax already withheld from the employee's pay. "
    "It is entered on Form 1040 line 25a and counts as a payment toward the year's tax.",
    "Form 1040 line 1a = total W-2 box 1 wages. Other income (interest, etc.) is "
    "added on later lines; line 9 = total income (the sum of all income lines).",
    "Form 1040 line 11 = adjusted gross income (AGI) = total income (line 9) minus "
    "adjustments to income from Schedule 1. With only W-2 wages and no adjustments, "
    "AGI equals total income.",
    "Form 1040 line 15 = taxable income = AGI (line 11) minus the deduction on "
    "line 12 (standard or itemized). Taxable income is never less than zero.",
    "Form 1040 line 16 = tax on taxable income. The IRS Tax Table is used when "
    "taxable income is under $100,000; the Tax Computation Worksheet (the bracket "
    "schedule) is used at $100,000 and above. Both implement the same marginal brackets.",
    "Amounts on Form 1040 may be rounded to whole dollars: drop amounts under 50 "
    "cents and increase amounts from 50 to 99 cents to the next dollar.",
    "Form 1040 line 24 = total tax. Line 25a = federal income tax withheld from "
    "W-2 box 2. Line 33 = total payments (withholding plus any credits and estimated "
    "payments).",
    "Compare Form 1040 line 33 (total payments) with line 24 (total tax). If "
    "payments exceed tax, the difference is your refund (line 34). If tax exceeds "
    "payments, you owe the difference (line 37).",
    "Form 1040 line flow: Line 1a = total W-2 box 1 wages. Line 9 = total "
    "income. Line 11 = adjusted gross income (AGI). Line 12 = standard "
    "deduction. Line 15 = taxable income (line 11 minus line 12, not below "
    "zero). Line 16 = tax computed on taxable income. Line 22/24 = total "
    "tax. Line 25a = federal income tax withheld from W-2 box 2. Line 33 = "
    "total payments. If line 33 > line 24, line 34 is the refund; otherwise "
    "line 37 is the amount you owe.",
]


def _one_sentence(*terms: str) -> str:
    """Regex matching a single sentence (no ``.``/newline within) that contains every
    TERM, in ANY order. Each term is a regex fragment; zero-width lookaheads from the
    sentence start assert each appears before the next period, so the match is
    order-independent. This is essential because the distiller phrases the same fact
    many ways — "Single filers are taxed at 22% on $48,475-..." (label first) and "The
    22% rate applies to ... for single filers" (label last) must both match — without
    that, a present-and-correct fact fails the check purely on word order.
    """
    lookaheads = "".join(rf"(?=[^.\n]*?(?:{t}))" for t in terms)
    return rf"(?is)(?:^|[.\n])\s*{lookaheads}[^.\n]*"


def labeled_bracket(label_alt: str, rate_pct: int, figure: int) -> str:
    """Regex for one distilled bracket fact binding a filing-status LABEL to a RATE
    and a characteristic dollar FIGURE within a single sentence, order-independent.
    Binding the status word is essential: a bracket's *numbers* survive via a
    same-range twin in another status even when this status's fact is dropped, so
    only a label-bound check catches the silent collapse.
    """
    return _one_sentence(label_alt, rf"{rate_pct}\s*(?:%|percent)", comma_optional(figure))


def labeled_deduction(label_alt: str, amount: int) -> str:
    """Regex binding a filing-status LABEL to its standard-deduction AMOUNT in one
    sentence (order-independent). Single and MFS are both $15,750, so the label
    binding is what keeps a collision from passing on the bare number."""
    return _one_sentence("deduction", label_alt, comma_optional(amount))


# Status label alternations, and the full TY2025 ladders as
# (rate%, characteristic figure that appears in that status's distilled fact):
# 10% -> bracket top ("up to $X"); 37% -> threshold ("above $X"); middle -> upper
# bound. Every status pair shares at least one range (Single/MFS share 10–32%;
# Single/MFS/HoH share 24% $103,350-$197,300; Single/HoH share 35% top $626,350),
# so each row is a collision candidate and each status's row is asserted distinctly.
STATUS_LABELS_RE = {
    "single": r"single",
    "mfj": r"married filing jointly|filing jointly|\bmfj\b",
    "mfs": r"married filing separately|filing separately|\bmfs\b",
    "hoh": r"head of household|\bhoh\b",
}
LADDERS = {
    "single": [(10, 11_925), (12, 48_475), (22, 103_350), (24, 197_300),
               (32, 250_525), (35, 626_350), (37, 626_350)],
    "mfj": [(10, 23_850), (12, 96_950), (22, 206_700), (24, 394_600),
            (32, 501_050), (35, 751_600), (37, 751_600)],
    "mfs": [(10, 11_925), (12, 48_475), (22, 103_350), (24, 197_300),
            (32, 250_525), (35, 375_800), (37, 375_800)],
    "hoh": [(10, 17_000), (12, 64_850), (22, 103_350), (24, 197_300),
            (32, 250_500), (35, 626_350), (37, 626_350)],
}
DEDUCTIONS = {"single": 15_750, "mfj": 31_500, "mfs": 15_750, "hoh": 23_625}

SUM_RULE = r"(?is)(?:sum|add|combin\w*|total)[^.\n]{0,90}each bracket"


def build_ruleset_distillation_case() -> dict:
    scn_dir = SRC / "ruleset_distillation"
    scn_dir.mkdir(parents=True, exist_ok=True)
    for i, text in enumerate(RULESET_DOCS, start=1):
        (scn_dir / f"rule_{i:02d}.txt").write_text(text, encoding="utf-8")

    checks: list[dict] = []
    # 1) Standard deduction for every filing status survives, label-bound.
    for status, amount in DEDUCTIONS.items():
        checks.append(regex_check(
            f"recall_std_deduction_{status}", labeled_deduction(STATUS_LABELS_RE[status], amount)))
    # 2) Every bracket of every status's full ladder survives distinctly — this is
    #    what the cross-status collapse breaks (whichever row it drops).
    for status, ladder in LADDERS.items():
        for rate, figure in ladder:
            checks.append(regex_check(
                f"recall_bracket_{status}_{rate}pct",
                labeled_bracket(STATUS_LABELS_RE[status], rate, figure)))
    # 3) The marginal/sum-of-brackets computation rule (without it the rows are
    #    isolated with no rule stitching them into a total).
    checks.append(regex_check("recall_sum_of_brackets_rule", SUM_RULE))
    checks.append(regex_check(
        "recall_brackets_are_marginal", r"(?is)marginal[^.]{0,80}(?:bracket|portion|rate)"))
    # 4) W-2 box mappings the agent needs to read the W-2 into the 1040.
    checks.append(regex_check(
        "recall_w2_box1_to_line1a", r"(?is)(?:box\s*1[^.]{0,80}1a|1a[^.]{0,80}box\s*1)"))
    checks.append(regex_check(
        "recall_w2_box2_to_line25a", r"(?is)(?:box\s*2[^.]{0,80}25a|25a[^.]{0,80}box\s*2)"))
    # 5) Form 1040 line flow: AGI, taxable income (floored), refund vs. owe.
    checks.append(regex_check(
        "recall_agi_line11", r"(?is)(?:AGI|adjusted gross income)[^.]{0,80}line\s*11|line\s*11[^.]{0,80}(?:AGI|adjusted gross)"))
    checks.append(regex_check(
        "recall_taxable_income_line15",
        r"(?is)taxable income[^.]{0,140}(?:line\s*11[^.]{0,30}line\s*12|11 minus[^.]{0,20}12)"))
    checks.append(regex_check(
        "recall_taxable_income_floored", r"(?is)(?:never less than zero|not below zero)"))
    checks.append(regex_check(
        "recall_refund_line34", r"(?is)(?:refund[^.]{0,40}line\s*34|line\s*34[^.]{0,40}refund)"))
    checks.append(regex_check(
        "recall_owe_line37", r"(?is)(?:owe[^.]{0,40}line\s*37|line\s*37[^.]{0,40}owe)"))
    # 6) Standard-vs-itemized "larger of" rule and the whole-dollar rounding rule.
    checks.append(regex_check(
        "recall_larger_of_std_or_itemized", r"(?is)larger[^.]{0,60}itemi"))
    # The rounding rule has TWO halves that the distiller can split apart: drop
    # amounts under 50c, AND round 50-99c up. Asserting them separately catches a
    # partial collapse where only the round-up half survives (observed in the live
    # audit) — a single combined check would pass on either half alone.
    # Order-independent (lookaheads, like the bracket checks): the distiller freely
    # reorders — "drop amounts under 50 cents" vs "amounts under 50 cents are dropped"
    # must both match, so a fixed drop->50c word order produces false negatives.
    checks.append(regex_check(
        "recall_rounding_drop_under_50c",
        r"(?is)(?:^|[.\n])\s*(?=[^.\n]*?(?:drop|disregard|ignore|round\w*\s+down))(?=[^.\n]*?(?:under|less than|below)?[^.\n]*?50\s*cents)[^.\n]*"))
    checks.append(regex_check(
        "recall_rounding_round_50_to_99_up",
        r"(?is)(?:50\s*(?:to|-|through)\s*99\s*cents|50\s*cents[^.\n]{0,40}next dollar|increase[^.\n]{0,40}next dollar|round\w*\s+up[^.\n]{0,40}(?:next dollar|50))"))

    case = {
        "id": "matt_tax_return_ruleset_distillation",
        "component": "graph_reader",
        "substrate": "vector",
        "embedder": "cached",
        "ingest_model": "openai/gpt-4o-mini",
        "ingest_state": "active",
        # FULL live-like write policy — the same collapse path the live /ingest
        # ran. merge_model -> LLM MergeJudge in the Deduper; conflict_model ->
        # ClaimExtractor + Claim/Semantic conflict detectors (reject-the-loser).
        # Both replay from committed cassettes offline; a live key records them.
        "merge_model": "openai/gpt-4o-mini",
        "conflict_model": "openai/gpt-4o-mini",
        "reader": "retrieving",
        "reader_top_k": 0,
        # This is a RECALL-integrity case: the prompt asks for the COMPLETE ruleset, so
        # every salient fact must surface, not just those nearest the single best hit.
        # The default rel_ratio=0.60 shape filter is a precision tool — it drops a fact
        # scoring under 60% of the top hit even when it is plainly on-topic (e.g.
        # "Taxable income is never less than zero." scores 0.317 vs a 0.68 top, so the
        # 0.41 cutoff drops it). Disable rel_ratio and let the abs_floor=0.30 existence
        # gate (kept at default) be the relevance guard; the case has no negative
        # control to protect, and every check is a must-be-present recall assertion.
        "reader_rel_ratio": 0.0,
        "seed_prompt": (
            "Give the complete TY2025 federal Form 1040 rules: the standard "
            "deduction and the full 10%-37% ordinary-income bracket schedule for "
            "every filing status (Single, Married filing jointly, Married filing "
            "separately, Head of household), how the per-bracket tax is summed, "
            "which W-2 boxes map to which 1040 lines, and the line-by-line flow "
            "from wages through taxable income to the refund or amount owed."
        ),
        "seeded_insight": {"via_ingestor": list(RULESET_DOCS)},
        "deterministic_checks": checks,
    }
    return {"case": case, "dir": HERE / "ruleset_distillation"}


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

    ruleset = build_ruleset_distillation_case()
    out = ruleset["dir"] / "case.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    ruleset_header = (
        "# GENERATED by _generate.py — edit that script, not this file.\n"
        "# Ruleset-distillation integrity case (graph_reader): ingests the exact 26\n"
        "# app/rules.py RULE_DOCUMENTS the live graph was seeded from, through the full\n"
        "# merge+conflict write policy, then asserts the reader surfaces every salient\n"
        "# fact distinctly — all four statuses' standard deductions and full bracket\n"
        "# ladders, the marginal/sum rule, the W-2 box mappings, and the 1040 line\n"
        "# flow. Guards against the cross-status collapse that silently drops a\n"
        "# filing-status bracket (e.g. Single 22% merged into the MFS twin).\n"
    )
    out.write_text(
        ruleset_header
        + yaml.safe_dump(ruleset["case"], sort_keys=False, allow_unicode=True, width=4096),
        encoding="utf-8",
    )
    written += 1

    print(f"wrote {written} case(s)")


if __name__ == "__main__":
    main()
