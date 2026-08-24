"""Schema validation of the generated corpus against the R4B models.

SKIPS when `fhir.resources` is absent -- `src/` does not depend on it.

The rest of this suite asks whether the corpus says the RIGHT things. These ask
whether it is VALID FHIR, which is a different question and had a different
answer: `Coverage.payor` is required in R4 and was missing from all 82 Coverage
resources. Seventy-seven passing tests could not see it, because none of them
were asking.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

pytest.importorskip("fhir.resources", reason="schema audit only")

import fhir_gen as G                                          # noqa: E402
import validate_fhir as V                                     # noqa: E402

from fhir.resources.R4B.coverage import Coverage              # noqa: E402


@pytest.fixture(scope="module")
def report():
    if not os.path.exists(V.NDJSON):
        G.generate(n=60, seed=5)
    return V.validate()


def test_every_bundle_is_valid_r4b(report):
    assert report["bundles_invalid"] == 0
    assert report["bundles_valid"] > 50


def test_every_resource_is_valid_r4b(report):
    assert report["invalid"] == {}
    assert report["total_valid"] > 150


def test_coverage_carries_a_payor(report):
    """REGRESSION TEST FOR THE BUG THIS AUDIT FOUND.

    `Coverage.payor` is required in R4 (1..*). It was absent, and the measure
    logic never noticed because continuous-enrolment only reads `period` -- a
    real FHIR server would have rejected every one on ingest.
    """
    covs = G._coverage("pat-x", [(__import__("datetime").date(2024, 1, 1),
                                  __import__("datetime").date(2024, 12, 31))])
    assert covs and covs[0]["payor"]
    Coverage.model_validate(covs[0])


def test_the_payor_is_a_display_not_a_dangling_reference():
    """There is no Organization resource in this corpus, so a `reference`
    pointing at one would be a dangling pointer dressed up as provenance.
    `display` says 'this is who paid, and we cannot resolve them'."""
    assert "display" in G.PAYOR
    assert "reference" not in G.PAYOR


def test_the_corpus_still_has_the_resource_counts_the_migration_tests_measure(
        report):
    """The fix had to add a FIELD, not a RESOURCE.

    Adding an `Organization` resource would have changed the resource counts,
    which the incremental and migration tests measure directly -- so the
    minimal correct fix was also the one that keeps those tests meaningful.
    """
    assert "Organization" not in report["valid"]
    assert report["valid"]["Coverage"] > 50
