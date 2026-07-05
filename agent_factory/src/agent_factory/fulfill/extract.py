"""U7 — the document-extraction seam.

Turns an uploaded document into candidate facts the gather loop can cover requirements with (the
``document:w2`` cover source). A generic :class:`Extractor` interface plus a tax W-2 extractor that
maps W-2 text -> ``box1_wages`` / ``box2_withholding`` / ``employer`` / ``employee_name``.

Text extraction mirrors the harness intake (``app/main.py:upload_w2``): a PDF is read with ``pypdf``
into text, a text file is decoded as-is, and the resulting text is mapped to fields here. Every
extracted value passes through the U3 validator before it becomes a candidate fact — the typed
boundary holds at intake (S6). Extraction NEVER raises on messy input: a missing field is left
unknown (asked later), an unreadable document yields a structured "no readable fields" result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .domain import Domain
from .validate import validate_field

# A whole-dollar/cents money token, e.g. ``40,000.00`` or ``3200``. Must START with a digit so a
# stray comma in the label text (e.g. "Wages,") is never mistaken for an amount.
_MONEY = r"(\d[\d,]*(?:\.\d{1,2})?)"


@dataclass
class ExtractResult:
    """Outcome of extracting a document. ``fields`` are the VALIDATED candidate values."""

    fields: dict[str, Any] = field(default_factory=dict)
    unreadable: bool = False
    notes: list[str] = field(default_factory=list)


def extract_text(document: Any) -> str:
    """Coerce a document (str text, or PDF/text bytes) to text, mirroring the harness upload path.

    ``str`` is returned as-is. ``bytes`` starting with ``%PDF`` are read via ``pypdf`` if available;
    other bytes are decoded as UTF-8 (replacement on error). Returns ``""`` on anything unreadable."""
    if isinstance(document, str):
        return document
    if isinstance(document, (bytes, bytearray)):
        raw = bytes(document)
        if raw[:4] == b"%PDF":
            try:
                import io

                from pypdf import PdfReader

                reader = PdfReader(io.BytesIO(raw))
                return "\n".join((p.extract_text() or "") for p in reader.pages)
            except Exception:  # noqa: BLE001 — unreadable PDF degrades to empty, never raises
                return ""
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _money(text: str) -> float | None:
    try:
        return float(text.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


class W2Extractor:
    """Map W-2 text to the four fields the 1040 needs. Picks Box 1 (taxable wages), never Box 3/5."""

    # Anchored on the box number + its description so Box 3/5 (SS/Medicare wages) can't masquerade
    # as wages, and Box 12a's amount is never read as Box 1.
    _PATTERNS = {
        "box1_wages": re.compile(r"Box\s*1\b[^\n]*?Wages[^\n]*?" + _MONEY, re.I),
        "box2_withholding": re.compile(r"Box\s*2\b[^\n]*?(?:withh|withheld)[^\n]*?" + _MONEY, re.I),
    }
    _EMPLOYER = re.compile(r"Box\s*c\b[^\n]*?Employer name[^\n]*?\.{2,}\s*(.+)", re.I)
    _EMPLOYEE = re.compile(r"Box\s*e\b[^\n]*?Employee name[^\n]*?\.{2,}\s*(.+)", re.I)

    def __init__(self, domain: Domain) -> None:
        self.domain = domain

    def extract(self, document: Any) -> ExtractResult:
        text = extract_text(document)
        if not text or not text.strip():
            return ExtractResult(unreadable=True, notes=["no readable text found in the document"])

        candidates: dict[str, Any] = {}
        for fieldname, pat in self._PATTERNS.items():
            m = pat.search(text)
            if m:
                num = _money(m.group(1))
                if num is not None:
                    candidates[fieldname] = num
        for fieldname, pat in (("employer", self._EMPLOYER), ("employee_name", self._EMPLOYEE)):
            m = pat.search(text)
            if m:
                value = m.group(1).strip()
                if value:
                    candidates[fieldname] = value

        result = ExtractResult()
        if not candidates:
            result.notes.append("no recognizable W-2 fields in the document")
            return result

        # The typed boundary (S6): a candidate only becomes a fact if it validates; drop+note others.
        for name, value in candidates.items():
            check = validate_field(self.domain, name, value)
            if check.ok:
                result.fields[name] = check.value
            else:
                result.notes.append(f"dropped {name}: {check.reason}")
        return result


def extractor_for(domain: Domain) -> W2Extractor:
    """Return the document extractor for ``domain``. (One domain, one extractor today; the registry
    is the seam where other document kinds plug in.)"""
    return W2Extractor(domain)
