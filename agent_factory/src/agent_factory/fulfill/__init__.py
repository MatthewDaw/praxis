"""af-fulfill — the runtime that drives an end user to complete a structured deliverable.

Sibling to af-build. Where af-build drives an *agent building software*, af-fulfill drives an
*end user supplying facts* against a Praxis requirement graph until the derived completeness gate
opens, then produces the deliverable. A domain is defined by data files (``domains/<id>/``); Praxis
tracks the per-session run.

This package is the generic, domain-agnostic runtime. The proving domain is ``tax-1040-2025``.
"""

from __future__ import annotations
