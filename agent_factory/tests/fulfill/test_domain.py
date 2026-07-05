"""U1 — domain-pack loader tests.

Happy: the relocated tax-1040-2025 pack loads with the documented shape. Error: each malformed-pack
fixture raises a DomainError naming the offending file and key.
"""

from __future__ import annotations

import pytest

from agent_factory.fulfill.domain import DomainError, load_domain
from tests.fulfill.conftest import DOMAIN_DIR, rewrite_yaml


def test_loads_tax_pack(domain):
    # 6 requirements, std-deduction + 4 bracket tables, 12 compute steps; references resolve.
    assert len(domain.requirements) == 6
    assert domain.project == "tax-1040-2025"
    assert domain.id == "tax-1040-2025"
    assert len(domain.compute_steps) == 12
    assert "standard_deduction" in domain.rules
    assert set(domain.rules["brackets"].keys()) == {
        "single",
        "married_filing_jointly",
        "married_filing_separately",
        "head_of_household",
    }
    # cross-file references all resolve (no DomainError raised by load).
    assert domain.step("line_16_tax").op == "marginal_tax"
    assert domain.requirement("T1")["field"] == "filing_status"


def test_field_schemas_and_line_map(domain):
    assert "filing_status" in domain.field_schemas
    assert domain.line_map["line_1a_wages"] == "1a"
    assert len(domain.cross_field) == 1  # withholding <= wages


def test_missing_rules_file_names_the_file(pack_factory):
    def drop_rules(dest):
        (dest / "rules.yaml").unlink()

    with pytest.raises(DomainError) as exc:
        load_domain(pack_factory(drop_rules))
    assert "rules" in str(exc.value).lower()


def test_unknown_compute_op_is_rejected(pack_factory):
    def corrupt(dest):
        rewrite_yaml(
            dest / "compute.yaml",
            lambda doc: doc["steps"].__setitem__(0, {**doc["steps"][0], "op": "frobnicate"}) or doc,
        )

    with pytest.raises(DomainError) as exc:
        load_domain(pack_factory(corrupt))
    msg = str(exc.value)
    assert "frobnicate" in msg and "compute" in msg


def test_line_map_referencing_unknown_step_is_rejected(pack_factory):
    def corrupt(dest):
        def t(doc):
            doc["line_map"]["line_does_not_exist"] = "99"
            return doc

        rewrite_yaml(dest / "template.yaml", t)

    with pytest.raises(DomainError) as exc:
        load_domain(pack_factory(corrupt))
    assert "line_does_not_exist" in str(exc.value)


def test_requirement_with_absent_field_is_rejected(pack_factory):
    def corrupt(dest):
        def t(doc):
            doc["requirements"][0]["field"] = "nonexistent_field"
            return doc

        rewrite_yaml(dest / "requirements.yaml", t)

    with pytest.raises(DomainError) as exc:
        load_domain(pack_factory(corrupt))
    assert "nonexistent_field" in str(exc.value)


def test_table_op_naming_absent_table_is_rejected(pack_factory):
    def corrupt(dest):
        def t(doc):
            del doc["standard_deduction"]
            return doc

        rewrite_yaml(dest / "rules.yaml", t)

    with pytest.raises(DomainError) as exc:
        load_domain(pack_factory(corrupt))
    assert "standard_deduction" in str(exc.value)


def test_load_real_domain_dir_directly():
    # the relocated pack at domains/tax-1040-2025 loads from its real path.
    domain = load_domain(DOMAIN_DIR)
    assert domain.id == "tax-1040-2025"
