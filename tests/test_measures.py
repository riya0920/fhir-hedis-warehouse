"""Tests for measure logic, which is to say tests for edge cases.

A measure is mostly boundary conditions: the day the period ends, the 45th day
of a gap, the birthday that moves someone out of an age band. Those are what
these test, plus one governance test asserting that measures reference value
sets rather than inline code lists.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import fhir_gen
import measures
import reference
import warehouse


# ---------------------------------------------------------------------------
class _Warehouse:
    """sqlite3.Connection has no __dict__, so the ndjson path rides alongside
    it rather than being attached to it."""

    def __init__(self, con, ndjson):
        self.con, self.ndjson = con, ndjson

    def execute(self, *a, **kw):
        return self.con.execute(*a, **kw)


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    d = tmp_path_factory.mktemp("wh")
    path = fhir_gen.generate(1500, seed=5, outdir=str(d))
    c, _counts = warehouse.load(path, str(d / "t.db"))
    return _Warehouse(c, path)


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------
def test_no_code_is_ever_stored_without_its_system(con):
    """A code without its system is meaningless: 44054006 is diabetes in
    SNOMED CT and something else entirely, or nothing, anywhere else."""
    for table in ("condition", "observation", "procedure", "immunization"):
        n = con.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE code_system IS NULL OR code IS NULL").fetchone()[0]
        assert n == 0, f"{table} has {n} rows with a code but no system"


def test_references_resolve_to_known_patients(con):
    for table in ("condition", "observation", "procedure", "encounter",
                  "immunization", "coverage"):
        orphans = con.execute(
            f"SELECT COUNT(*) FROM {table} t "
            f"LEFT JOIN patient p ON p.patient_id = t.patient_id "
            f"WHERE p.patient_id IS NULL").fetchone()[0]
        assert orphans == 0, f"{table} has {orphans} rows referencing no patient"


def test_value_sets_are_loaded_as_data(con):
    n = con.execute("SELECT COUNT(DISTINCT value_set_id) FROM value_set").fetchone()[0]
    assert n == len(warehouse.VALUE_SETS)
    assert con.execute("SELECT COUNT(*) FROM value_set").fetchone()[0] > 10


def test_measures_reference_value_sets_not_inline_code_lists():
    """Governance test. Measure SQL that contains a literal code cannot be
    updated when a value set is revised without someone grepping every measure
    -- which is how a measure silently keeps using last year's codes.

    src/reference.py is exempt BY DESIGN: it hard-codes its lists precisely so
    a typo in a seed row shows up as a reconciliation mismatch rather than
    being shared by both implementations.
    """
    src = open(os.path.join(ROOT, "src", "measures.py"), encoding="utf-8").read()
    body = src.split('"""', 2)[-1]          # skip the module docstring
    suspicious = re.findall(r'"\d{5,7}-?\d?"', body)      # LOINC/SNOMED/CPT shapes
    assert not suspicious, f"inline code literals in measures.py: {suspicious}"


# ---------------------------------------------------------------------------
# Continuous enrolment
# ---------------------------------------------------------------------------
def test_full_year_is_continuous():
    ok, gaps, longest = measures.continuous_enrolment(
        [("2024-01-01", "2024-12-31")])
    assert ok and gaps == 0 and longest == 0


def test_one_gap_of_exactly_45_days_is_allowed():
    """The boundary. HEDIS permits one gap of up to 45 days; 45 is in, 46 is
    out, and an implementation that gets this backwards moves the denominator
    for every churning member in the plan."""
    ok, _g, longest = measures.continuous_enrolment(
        [("2024-01-01", "2024-05-31"), ("2024-07-16", "2024-12-31")])
    assert longest == 45
    assert ok


def test_one_gap_of_46_days_is_not_allowed():
    ok, _g, longest = measures.continuous_enrolment(
        [("2024-01-01", "2024-05-31"), ("2024-07-17", "2024-12-31")])
    assert longest == 46
    assert not ok


def test_two_gaps_disqualify_even_if_each_is_short():
    ok, gaps, _l = measures.continuous_enrolment(
        [("2024-01-01", "2024-03-31"), ("2024-04-20", "2024-08-31"),
         ("2024-09-20", "2024-12-31")])
    assert gaps == 2
    assert not ok


def test_mid_year_enrollee_is_not_continuously_enrolled():
    ok, _g, _l = measures.continuous_enrolment([("2024-07-01", "2024-12-31")])
    assert not ok


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------
def test_age_is_taken_as_of_the_end_of_the_measurement_year():
    assert measures.age_as_of("1949-01-01", date(2024, 12, 31)) == 75
    assert measures.age_as_of("1948-12-31", date(2024, 12, 31)) == 76
    assert measures.age_as_of("1949-12-31", date(2024, 12, 31)) == 75


def test_birthday_after_year_end_does_not_count():
    assert measures.age_as_of("1949-01-01", date(2024, 6, 30)) == 75


# ---------------------------------------------------------------------------
# The planted edge cases
# ---------------------------------------------------------------------------
def test_every_planted_edge_case_behaves_as_specified(con):
    r = measures.cdc_hba1c(con)
    for name, exp in fhir_gen.EDGE_CASES.items():
        assert (name in r.denominator_ids) == exp["expected_denominator"], (
            f"{name} denominator: {exp['why']}")
        assert (name in r.numerator_ids) == exp["expected_numerator"], (
            f"{name} numerator: {exp['why']}")


def test_numerator_event_one_day_late_is_excluded(con):
    """The spec decides period boundaries, not intuition. A test on 1 January
    of the following year is not in the measurement year, however obviously
    the patient 'had their test'."""
    r = measures.cdc_hba1c(con)
    assert "EC3-late-event" in r.denominator_ids
    assert "EC3-late-event" not in r.numerator_ids


def test_hospice_removes_from_the_denominator_not_just_the_numerator(con):
    """An exclusion removes the member from the measure entirely. Treating it
    as a numerator failure would penalise the plan for not screening a patient
    it correctly did not screen."""
    r = measures.cdc_hba1c(con)
    assert "EC4-hospice" not in r.denominator_ids
    assert "EC4-hospice" in r.excluded_ids


# ---------------------------------------------------------------------------
# Waterfall and rate integrity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["CDC-A1C", "BCS", "CIS-DTaP"])
def test_waterfall_is_monotonically_non_increasing(con, key):
    r = measures.MEASURES[key](con)
    counts = [n for _label, n in r.waterfall]
    assert counts == sorted(counts, reverse=True), (
        f"{key} waterfall goes up somewhere: {r.waterfall}")


@pytest.mark.parametrize("key", ["CDC-A1C", "BCS", "CIS-DTaP"])
def test_numerator_is_a_subset_of_the_denominator(con, key):
    r = measures.MEASURES[key](con)
    assert r.numerator_ids <= r.denominator_ids


@pytest.mark.parametrize("key", ["CDC-A1C", "BCS", "CIS-DTaP"])
def test_rate_matches_the_waterfall_endpoints(con, key):
    r = measures.MEASURES[key](con)
    assert r.rate == pytest.approx(
        len(r.numerator_ids) / len(r.denominator_ids))


def test_exclusions_never_appear_in_the_denominator(con):
    for key in measures.MEASURES:
        r = measures.MEASURES[key](con)
        assert r.excluded_ids & r.denominator_ids == set()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def test_pipeline_reconciles_to_the_independent_implementation(con):
    results = {k: fn(con) for k, fn in measures.MEASURES.items()}
    out = reference.reconcile(con.ndjson, con, results, None)
    for key, r in out.items():
        assert r["mismatches"] == 0, (
            f"{key}: {r['mismatches']} of {r['n']} disagree; {r['detail'][:3]}")


def test_care_gap_list_contains_only_non_compliant_members(con):
    r = measures.cdc_hba1c(con)
    gaps = measures.care_gaps(con, r)
    ids = {g["patient_id"] for g in gaps}
    assert ids == r.denominator_ids - r.numerator_ids
    assert all(g["missing"] for g in gaps)
