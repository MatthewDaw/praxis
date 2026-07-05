"""U8 — the form-fill seam + provenance + assumption receipt + content hash (S2, S5, S7, S10).

Writes the evaluator's computed lines into the deliverable (the official 1040 PDF), carrying:

- **S2 provenance** — every written value is tagged with where it came from (``w2`` | ``user`` |
  ``default`` | ``engine`` | ``kg``); a value with no non-LLM provenance is a hard assertion failure
  (the LLM never produces a number).
- **S5 assumption receipt** — one line per requirement closed by a policy default, citing the
  justification from ``policy.yaml``.
- **S10 content hash** — a sha256 over the computed lines + identity fields + receipt, so identical
  validated inputs yield an identical hash shown in the UI.

Field-name strategy (S7): the official IRS AcroForm field names shift year to year, so they are NOT
hardcoded — the seam resolves the field map from the PDF at startup when ``pypdf`` + a template are
present, and otherwise renders a faithful 1040-shaped PDF (here a stdlib generator, so the path never
depends on reportlab being installed). The HASH is over the DATA, never the rendered bytes, so it is
stable across rendering backends.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .domain import Domain

# The closed provenance vocabulary (S2). A populated line whose source is outside this set — most
# importantly a hypothetical "llm" source — is rejected: the LLM never authors a number.
ALLOWED_SOURCES = frozenset({"engine", "kg", "w2", "user", "default"})


class ProvenanceError(AssertionError):
    """A computed line carries a value with no valid non-LLM provenance (S2 boundary breach)."""


@dataclass
class Deliverable:
    """The produced 1040: structured line items, provenance, the receipt, a stable hash, the bytes."""

    line_items: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)
    receipt: list[dict[str, Any]] = field(default_factory=list)
    content_hash: str = ""
    source_form: str = ""
    pdf_bytes: bytes = b""

    def line(self, line_key: str) -> dict | None:
        for it in self.line_items:
            if it["line_key"] == line_key:
                return it
        return None


def _line_source(step, cover_sources: dict[str, str]) -> str:
    """The provenance of a computed line: a line that directly carries a single covered field inherits
    that field's cover source; everything the engine derives is ``engine``."""
    if step.op == "sum":
        return cover_sources.get(str(step.spec.get("field")), "engine")
    if step.op == "copy":
        frm = step.spec.get("from")
        if isinstance(frm, dict) and "field" in frm:
            return cover_sources.get(str(frm["field"]), "engine")
    return "engine"


def build_line_items(domain: Domain, results: dict, cover_sources: dict[str, str]) -> list[dict]:
    """One entry per ``template.line_map`` line: its 1040 line number, value, basis, and provenance.

    Raises :class:`ProvenanceError` if any populated line resolves to a source outside
    :data:`ALLOWED_SOURCES` (the S2 boundary check)."""
    items: list[dict] = []
    for line_key, form_line in domain.line_map.items():
        step = domain.step(line_key)
        cell = results.get(line_key) or {}
        value = cell.get("value")
        source = _line_source(step, cover_sources) if step else "engine"
        if value is not None and source not in ALLOWED_SOURCES:
            raise ProvenanceError(
                f"line {line_key!r} value {value!r} has invalid provenance {source!r} "
                f"(not one of {sorted(ALLOWED_SOURCES)})"
            )
        items.append({
            "line_key": line_key,
            "line": form_line,
            "label": step.label if step else line_key,
            "value": value,
            "basis": cell.get("basis"),
            "source": source,
        })
    return items


def build_receipt(domain: Domain, defaulted_fields: list[str], facts: dict) -> list[dict]:
    """One receipt line per requirement closed by a default, citing the policy justification (S5)."""
    defaults = domain.policy.get("defaults") or {}
    receipt: list[dict] = []
    for fname in defaulted_fields:
        spec = defaults.get(fname) or {}
        receipt.append({
            "field": fname,
            "value": facts.get(fname, spec.get("value")),
            "justification": spec.get("justification", "closed by default"),
        })
    return receipt


def _identity_fields(domain: Domain, facts: dict) -> dict[str, Any]:
    """The non-computed header fields from ``template.identity_fields`` (e.g. taxpayer name, the
    rendered filing-status label)."""
    out: dict[str, Any] = {}
    for name, spec in (domain.template.get("identity_fields") or {}).items():
        src = (spec or {}).get("from") or {}
        fld = src.get("field")
        value = facts.get(fld) if fld else None
        render = (spec or {}).get("render")
        if render and value is not None:
            # render via a dotted rules path, e.g. "rules.filing_status_labels".
            table = domain.rules
            for part in str(render).split(".")[1:]:  # drop the leading "rules"
                table = (table or {}).get(part, {})
            value = (table or {}).get(value, value)
        out[name] = value
    return out


def content_hash(line_items: list[dict], identity: dict, receipt: list[dict]) -> str:
    """S10: deterministic sha256 over the computed lines + identity fields + receipt."""
    payload = {
        "computed_lines": {it["line_key"]: it["value"] for it in line_items},
        "identity_fields": identity,
        "assumption_receipt": [
            {"field": r["field"], "value": r["value"]} for r in receipt
        ],
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def produce_deliverable(
    domain: Domain,
    results: dict,
    *,
    facts: dict,
    cover_sources: dict[str, str],
    defaulted_fields: list[str],
) -> Deliverable:
    """Assemble the deliverable: provenance-tagged line items, the receipt, the hash, and the PDF.

    ``cover_sources`` maps field -> how it was covered (``w2`` / ``user`` / ``default``);
    ``defaulted_fields`` are the requirements closed by a default (the receipt's source). Fail-loud on
    a provenance breach (S2)."""
    line_items = build_line_items(domain, results, cover_sources)
    receipt = build_receipt(domain, defaulted_fields, facts)
    identity = _identity_fields(domain, facts)
    h = content_hash(line_items, identity, receipt)
    pdf_bytes, source_form = _render(domain, line_items, identity, receipt, h)
    provenance = {it["line_key"]: it["source"] for it in line_items}
    return Deliverable(
        line_items=line_items,
        provenance=provenance,
        receipt=receipt,
        content_hash=h,
        source_form=source_form,
        pdf_bytes=pdf_bytes,
    )


# --------------------------------------------------------------------------- rendering

def _render(domain, line_items, identity, receipt, result_hash) -> tuple[bytes, str]:
    """Render the deliverable to PDF bytes. Resolves a real AcroForm template if pypdf + the file are
    present (S7); otherwise renders a 1040-shaped PDF with the stdlib generator (always available)."""
    official = _try_official_acroform(domain, line_items, identity)
    if official is not None:
        return official, "official_irs_acroform"
    return _render_shaped(domain, line_items, identity, receipt, result_hash), "rendered_1040_shaped"


def _try_official_acroform(domain, line_items, identity) -> bytes | None:
    """Fill the official IRS AcroForm if both the template PDF and pypdf are available (else None).

    The field map is resolved from the PDF AT STARTUP (S7), not hardcoded — drift falls through to the
    shaped renderer."""
    source = (domain.template.get("source_form") or {})
    primary = source.get("primary")
    if not primary:
        return None
    template_path = domain.path / str(primary)
    if not template_path.exists():
        return None
    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(str(template_path))
        acro = reader.get_fields() or {}
        values = {}
        for it in line_items:
            if it["value"] is None:
                continue
            # match a field whose name contains the 1040 line number (best-effort resolve).
            for fname in acro:
                if str(it["line"]) in str(fname):
                    values[fname] = _money(it["value"])
                    break
        writer = PdfWriter()
        writer.append(reader)
        for page in writer.pages:
            writer.update_page_form_field_values(page, values)
        import io

        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — any AcroForm drift falls back to the shaped renderer
        return None


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _render_shaped(domain, line_items, identity, receipt, result_hash) -> bytes:
    """Build a faithful 1040-shaped PDF with the stdlib (no reportlab dependency)."""
    lines: list[str] = []
    lines.append("Form 1040 - U.S. Individual Income Tax Return  (Tax year 2025)")
    lines.append("Educational prototype - test data only. Not a filed return, not tax advice.")
    lines.append("")
    name = identity.get("taxpayer_name") or "(name not provided)"
    lines.append(f"Taxpayer: {name}")
    fs = identity.get("filing_status")
    if fs:
        lines.append(f"Filing status: {fs}")
    lines.append("")
    for it in line_items:
        val = _money(it["value"]) if it["value"] is not None else "(unknown)"
        lines.append(f"Line {it['line']:>4}  {it['label'][:48]:<48} {val:>14}   [{it['source']}]")
    lines.append("")
    lines.append("Assumption receipt - values inferred without asking you:")
    if receipt:
        for r in receipt:
            lines.append(f"  - {r['field']}: {r['value']} ({r['justification']})")
    else:
        lines.append("  - None: every value came from your W-2 or your answers.")
    lines.append("")
    lines.append(f"Reproducibility hash (sha256): {result_hash}")
    return _pdf_from_text_lines(lines)


def _pdf_from_text_lines(text_lines: list[str]) -> bytes:
    """A minimal, valid single-page PDF rendering ``text_lines`` in Helvetica (stdlib only)."""
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    content = ["BT", "/F1 9 Tf", "1 0 0 1 36 750 Tm", "11 TL"]
    for ln in text_lines:
        content.append(f"({esc(ln)}) Tj")
        content.append("T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)
