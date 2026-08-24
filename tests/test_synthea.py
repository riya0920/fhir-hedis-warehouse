"""Bugs that only real, foreign data exposed -- and the Synthea run itself.

The first three classes of test need NO Synthea output and always run. They pin
bugs that were invisible for as long as this repository was the only source of
data, and each one failed silently rather than raising:

  * `urn:uuid:` references matched no patient
  * `performedPeriod` was not read at all
  * a value set contained a SNOMED code meaning something else entirely

The integration tests at the bottom skip unless a Synthea population has been
generated (`python run_synthea.py --generate`, needs Java -- see TOOLCHAIN.md).
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import fhir_gen as G
import measures as M
import reference as R
import synthea as S
import warehouse as W


# ---------------------------------------------------------------- references
def test_urn_uuid_references_resolve():
    """REGRESSION. Synthea writes `urn:uuid:...` in transaction bundles.

    The old `_ref_id` split on "/" and returned the whole string, which matched
    no `Patient.id`. Nothing raised: every clinical resource joined to nobody,
    every denominator collapsed to zero, and a measure reporting 0% reads as a
    finding rather than a failure.
    """
    got = W._ref_id({"reference":
                     "urn:uuid:ef709d42-d538-6d4e-3873-7c48f2479645"})
    assert got == "ef709d42-d538-6d4e-3873-7c48f2479645"


def test_relative_and_absolute_references_still_resolve():
    assert W._ref_id({"reference": "Patient/P000123"}) == "P000123"
    assert W._ref_id({"reference":
                      "http://ex.org/fhir/Patient/P000123"}) == "P000123"
    assert W._ref_id(None) is None
    assert W._ref_id({}) is None


# --------------------------------------------------------------- choice types
def test_performed_period_is_read():
    """REGRESSION. FHIR choice types let one field be expressed two ways, and
    a server is conformant either way.

    100% of Synthea procedures use `performedPeriod`. Reading only
    `performedDateTime` is not a partial implementation, it is a silent one:
    the column comes back NULL and every date comparison downstream fails.
    """
    assert W._choice_date({"performedPeriod": {"start": "2024-06-01"}},
                          "performed") == "2024-06-01"


def test_datetime_form_still_wins_when_both_are_present():
    got = W._choice_date({"performedDateTime": "2024-01-01",
                          "performedPeriod": {"start": "2023-01-01"}},
                         "performed")
    assert got == "2024-01-01"


def test_a_missing_date_is_none_not_an_exception():
    assert W._choice_date({}, "performed") is None


def test_a_measure_survives_an_undated_resource():
    """One undated record in a million-row feed must not take down the run.

    It cannot be shown to fall inside a lookback, so it does not count -- but
    the correct behaviour is to exclude it, not to raise.
    """
    from datetime import date
    assert M._d(None) is None
    assert M._d("2024-06-01") == date(2024, 6, 1)


# ------------------------------------------------------------ the wrong code
def test_the_mastectomy_value_set_is_not_snomed_428251008():
    """THE BUG WORTH READING TWICE.

    This value set claimed SNOMED 428251008 was "History of bilateral
    mastectomy". In SNOMED CT that code means **History of appendectomy**.

    The generator emitted 428251008 and the value set looked for 428251008, so
    they agreed perfectly and no test could see it. On real Synthea data 28
    appendectomy records matched, and 4 of those patients were BCS-eligible
    women wrongly excluded from breast-cancer screening -- the denominator went
    from 97 to 101 once the code was corrected.
    """
    bad = ("http://snomed.info/sct", "428251008")

    for _oid, (name, members) in W.VALUE_SETS.items():
        if name == "Bilateral Mastectomy":
            assert all((s, c) != bad for s, c, _d in members)

    assert bad not in R.MASTECTOMY
    assert G.CODES["mastectomy_bilateral"][:2] != bad


def test_the_replacement_is_obviously_not_a_terminology_binding():
    """The replacement was NOT guessed, deliberately.

    Guessing is what caused the bug, and a second plausible-looking wrong
    SNOMED code would be worse than an obviously-local one, because it would
    look right. The real binding needs VSAC.
    """
    system, code, _display = G.CODES["mastectomy_bilateral"]
    assert system.startswith("urn:healthcare-hm:")
    assert "EXAMPLE" in code
    assert "snomed" not in system.lower()


def test_the_exclusion_still_works_on_this_project_s_own_data():
    """Fixing the code must not disable the exclusion it was meant to express."""
    codes = {(s, c) for _o, (n, mem) in W.VALUE_SETS.items()
             if n == "Bilateral Mastectomy" for s, c, _d in mem}
    assert codes == {(G.CODES["mastectomy_bilateral"][0],
                      G.CODES["mastectomy_bilateral"][1])}


# -------------------------------------------------------------- integration
SYN_OUTPUT = os.path.join(ROOT, "synthea_out", "output")

synthea_only = pytest.mark.skipif(
    not os.path.isdir(SYN_OUTPUT),
    reason="no Synthea population; run `python run_synthea.py --generate`")


@synthea_only
def test_synthea_emits_no_coverage_resource():
    """Documents SEAM 2. Continuous enrolment gates every denominator, and
    Synthea's FHIR export has no Coverage at all -- so this pipeline has to
    manufacture it from the CSV export."""
    seen = set()
    for path in S.bundle_paths(SYN_OUTPUT)[:20]:
        if not S._is_patient_bundle(path):
            continue
        with open(path, encoding="utf-8") as fh:
            bundle = json.load(fh)
        for entry in bundle.get("entry", []):
            seen.add(entry["resource"]["resourceType"])
    assert seen, "no resources read at all"
    assert "Coverage" not in seen


@synthea_only
def test_derived_coverage_is_tagged_as_derived():
    """A resource this pipeline manufactured must never be mistaken for one
    the generator produced."""
    cov = S.coverage_from_payer_transitions(SYN_OUTPUT)
    assert cov
    sample = next(iter(cov.values()))[0]
    tags = sample["meta"]["tag"]
    assert any(t["code"] == "derived" for t in tags)
    assert sample["payor"], "Coverage.payor is required in R4"


@pytest.fixture(scope="module")
def synthea_results():
    """Loaded ONCE. Each run reads 686 bundles and ~984k entries twice over,
    so letting every test call it turns a 30-second check into five minutes."""
    import run_synthea
    return run_synthea.run(rebuild=False)[1]


@synthea_only
def test_the_project_value_sets_miss_most_of_the_real_data(synthea_results):
    """THE COST OF ILLUSTRATIVE VALUE SETS, AS A NUMBER.

    Hospice matched zero rows -- a required exclusion that excluded nobody --
    and mammography matched 3 rows against 118.
    """
    proj = synthea_results["project"]["value_sets"]
    obs = synthea_results["observed"]["value_sets"]

    assert proj["Hospice Encounter"]["matching_rows"] == 0
    assert obs["Hospice Encounter"]["matching_rows"] > 50
    assert obs["Mammography"]["matching_rows"] > \
        10 * max(proj["Mammography"]["matching_rows"], 1)


@synthea_only
def test_the_measured_rate_depends_heavily_on_the_value_set(synthea_results):
    """Identical patients, different codes, a rate that moves more than 15
    percentage points. Nothing about the care changed."""
    a = synthea_results["project"]["measures"]["CDC-A1C"]["rate"]
    b = synthea_results["observed"]["measures"]["CDC-A1C"]["rate"]
    assert a is not None and b is not None
    assert abs(a - b) > 0.10
