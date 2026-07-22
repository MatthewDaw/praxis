"""U1 — the domain-pack loader.

A domain is DATA, not code (KTD4): seven YAML files under ``domains/<id>/`` describe the
requirement set, the field schemas, the rule tables, the calculation graph, the output template,
and the runtime policy. :func:`load_domain` parses them into typed, in-memory objects the rest of
the runtime (U2-U9) consumes, and validates cross-file integrity AT LOAD so a malformed pack fails
loud — naming the offending file and key — rather than surfacing as a confusing runtime error deep
in the evaluator.

The structural validation is the contract the loader owns:

- every ``requirements[*].field`` exists in ``fields``;
- every ``compute`` step ``op`` is in the closed vocabulary;
- every ``table_lookup`` / ``marginal_tax`` step names a table present in ``rules``;
- every ``template.line_map`` key is a real ``compute`` step id.

Shape drift is tolerated the way :mod:`agent_factory.build_target` tolerates it — a missing optional
key yields a safe default, never a crash — but a *referential* error (a requirement pointing at a
non-existent field, an unknown op) is a hard, named failure, because it would silently corrupt a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The closed calculation-graph vocabulary (KTD3). A compute step whose ``op`` is outside this set is
# rejected at load — the evaluator implements exactly these and nothing else.
COMPUTE_OPS: frozenset[str] = frozenset(
    {"sum", "add", "subtract", "copy", "const", "table_lookup", "marginal_tax"}
)
# The closed post-op vocabulary applied to a step's result.
POST_OPS: frozenset[str] = frozenset({"clamp_min", "round"})
# Ops that name a rule table (and so must resolve against ``rules``).
TABLE_OPS: frozenset[str] = frozenset({"table_lookup", "marginal_tax"})

# The seven files a pack is made of. ``manifest`` wires the rest; the manifest's ``files`` block may
# override the default names, but these are the defaults.
_DEFAULT_FILES = {
    "requirements": "requirements.yaml",
    "fields": "fields.yaml",
    "rules": "rules.yaml",
    "compute": "compute.yaml",
    "template": "template.yaml",
    "policy": "policy.yaml",
}


class DomainError(ValueError):
    """A pack is structurally invalid. The message names the offending file and key."""


@dataclass(frozen=True)
class ComputeStep:
    """One ordered node of the calculation graph. ``op`` is in :data:`COMPUTE_OPS`; ``spec`` holds
    the op-specific inputs verbatim (``inputs`` / ``from`` / ``over`` / ``table`` / ``key`` / ...).
    ``post`` is the ordered list of post-ops (each a ``{op: arg}`` dict)."""

    id: str
    label: str
    op: str
    spec: dict[str, Any]
    post: list[dict[str, Any]]


@dataclass(frozen=True)
class Domain:
    """A parsed, validated domain pack. The sub-objects are the raw (but integrity-checked) YAML
    structures, plus :attr:`compute_steps` as typed nodes and convenience accessors."""

    path: Path
    manifest: dict[str, Any]
    requirements: list[dict[str, Any]]
    fields: dict[str, Any]
    rules: dict[str, Any]
    compute: dict[str, Any]
    template: dict[str, Any]
    policy: dict[str, Any]
    compute_steps: list[ComputeStep] = field(default_factory=list)

    # --- identity -----------------------------------------------------------
    @property
    def id(self) -> str:
        return str(self.manifest.get("id") or self.path.name)

    @property
    def project(self) -> str:
        """The bare Praxis project name. Requirement facts get ``source = prd-<project>``."""
        return str(self.manifest.get("project") or self.id)

    @property
    def source(self) -> str:
        """The Praxis ``source`` requirement facts are seeded under."""
        return f"prd-{self.project}"

    @property
    def deliverable_screen_id(self) -> str:
        """The single surface every requirement ``renders`` onto (D9). Defaults to ``deliverable``."""
        deliverable = self.manifest.get("deliverable") or {}
        return str(deliverable.get("surface_id") or "deliverable")

    @property
    def deliverable_label(self) -> str:
        deliverable = self.manifest.get("deliverable") or {}
        return str(deliverable.get("human_label") or self.id)

    # --- accessors the runtime leans on ------------------------------------
    @property
    def field_schemas(self) -> dict[str, Any]:
        return self.fields.get("fields") or {}

    @property
    def cross_field(self) -> list[dict[str, Any]]:
        return self.fields.get("cross_field") or []

    @property
    def line_map(self) -> dict[str, str]:
        return self.template.get("line_map") or {}

    def __post_init__(self) -> None:
        # id->node lookups built once so step()/requirement() are O(1) inside per-line / per-dep
        # loops (formfill.build_line_items, requirements.deps_met) instead of O(n) linear scans.
        step_index: dict[str, ComputeStep] = {}
        for s in self.compute_steps:
            step_index.setdefault(s.id, s)  # first-match wins, matching the old linear scan
        req_index: dict[str, dict[str, Any]] = {}
        for r in self.requirements:
            req_index.setdefault(str(r.get("id")), r)
        object.__setattr__(self, "_step_index", step_index)
        object.__setattr__(self, "_req_index", req_index)

    def step(self, step_id: str) -> ComputeStep | None:
        return self._step_index.get(step_id)

    def requirement(self, req_id: str) -> dict[str, Any] | None:
        return self._req_index.get(req_id)


def _read_yaml(path: Path, label: str) -> Any:
    """Parse one YAML file, raising a named :class:`DomainError` on a missing/broken file."""
    if not path.is_file():
        raise DomainError(f"{label}: missing file {path.name!r} (expected at {path})")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # malformed YAML — name the file
        raise DomainError(f"{label}: could not parse {path.name!r}: {exc}") from exc


def _build_compute_steps(compute: dict[str, Any]) -> list[ComputeStep]:
    """Adapt the raw ``compute.steps`` list into typed :class:`ComputeStep` nodes (tolerant of a
    missing label/post; op-specific keys ride in ``spec``)."""
    steps_raw = compute.get("steps") or []
    out: list[ComputeStep] = []
    for raw in steps_raw:
        if not isinstance(raw, dict):
            raise DomainError(f"compute: each step must be a mapping, got {type(raw).__name__}")
        step_id = str(raw.get("id") or "")
        if not step_id:
            raise DomainError("compute: a step is missing its 'id'")
        spec = {k: v for k, v in raw.items() if k not in ("id", "label", "op", "post")}
        out.append(
            ComputeStep(
                id=step_id,
                label=str(raw.get("label") or step_id),
                op=str(raw.get("op") or ""),
                spec=spec,
                post=list(raw.get("post") or []),
            )
        )
    return out


def _validate(domain: Domain) -> None:
    """Cross-file integrity. Raises :class:`DomainError` naming the offending file+key on the first
    referential break — the loader's whole reason to exist."""
    fields = domain.field_schemas
    if not isinstance(fields, dict) or not fields:
        raise DomainError("fields: 'fields' map is empty or missing")

    # requirements[*].field must exist in fields
    for req in domain.requirements:
        rid = req.get("id", "<no-id>")
        fname = req.get("field")
        if fname is not None and fname not in fields:
            raise DomainError(
                f"requirements: requirement {rid!r} references field {fname!r} "
                f"absent from fields.yaml"
            )

    # compute ops in the closed vocabulary; table ops resolve against rules; post-ops valid
    step_ids = {s.id for s in domain.compute_steps}
    for s in domain.compute_steps:
        if s.op not in COMPUTE_OPS:
            raise DomainError(
                f"compute: step {s.id!r} has unknown op {s.op!r}; "
                f"allowed: {', '.join(sorted(COMPUTE_OPS))}"
            )
        if s.op in TABLE_OPS:
            table = s.spec.get("table") or s.spec.get("schedule")
            if not table or table not in (domain.rules or {}):
                raise DomainError(
                    f"compute: step {s.id!r} ({s.op}) names table {table!r} "
                    f"absent from rules.yaml"
                )
        for post in s.post:
            if not isinstance(post, dict) or not post:
                raise DomainError(f"compute: step {s.id!r} has a malformed post-op {post!r}")
            for pop in post:
                if pop not in POST_OPS:
                    raise DomainError(
                        f"compute: step {s.id!r} has unknown post-op {pop!r}; "
                        f"allowed: {', '.join(sorted(POST_OPS))}"
                    )

    # template.line_map keys must be real compute step ids
    for line_key in domain.line_map:
        if line_key not in step_ids:
            raise DomainError(
                f"template: line_map key {line_key!r} is not a compute step id"
            )


def load_domain(path: str | Path) -> Domain:
    """Parse and structurally validate the domain pack at ``path`` into a :class:`Domain`.

    The manifest is read first (it wires the rest of the pack via its ``files`` block, defaulting to
    the canonical names). Each referenced file is parsed; then cross-file integrity is checked
    (:func:`_validate`). On any structural defect a :class:`DomainError` is raised naming the file and
    key — the loader never returns a half-valid pack.
    """
    root = Path(path)
    if not root.is_dir():
        raise DomainError(f"manifest: domain path {root} is not a directory")

    manifest = _read_yaml(root / "manifest.yaml", "manifest")
    if not isinstance(manifest, dict):
        raise DomainError("manifest: manifest.yaml must be a mapping")

    files = {**_DEFAULT_FILES, **(manifest.get("files") or {})}
    parsed: dict[str, Any] = {}
    for key, fname in files.items():
        parsed[key] = _read_yaml(root / str(fname), key)

    requirements_doc = parsed["requirements"] or {}
    compute_doc = parsed["compute"] or {}

    domain = Domain(
        path=root,
        manifest=manifest,
        requirements=list(requirements_doc.get("requirements") or []),
        fields=parsed["fields"] or {},
        rules=parsed["rules"] or {},
        compute=compute_doc,
        template=parsed["template"] or {},
        policy=parsed["policy"] or {},
        compute_steps=_build_compute_steps(compute_doc),
    )
    _validate(domain)
    return domain
