"""Load Synthea output into this warehouse, and be honest about the seams.

WHY THIS MATTERS MORE THAN IT LOOKS
------------------------------------
Every measure in this project was written against bundles that this repository
also wrote. That is a closed loop: the generator emits the codes the value sets
look for, at the grain the loader expects, with the references the joins
assume. A pipeline can pass every test in that arrangement and still be unable
to read anybody else's data.

Synthea is the reference synthetic-data generator for exactly this kind of
work, and it is data this repository did NOT write. Pointing the pipeline at it
is the data-level version of the same discipline the rest of the portfolio
applies to code: check yourself against an independent implementation.

It found three seams immediately, and none of them raised an exception.

SEAM 1 -- REFERENCES
Synthea writes `urn:uuid:...` references in transaction bundles. The loader's
`_ref_id` split on "/", which returns the whole string, which then matches no
`Patient.id`. Nothing errors; every clinical resource joins to nobody and every
denominator collapses to zero. A measure reporting 0% looks like a finding.
Fixed in `warehouse._ref_id`.

SEAM 2 -- NO COVERAGE RESOURCE AT ALL
Synthea's FHIR export emits no `Coverage`. Continuous enrolment is the gate on
every HEDIS denominator, so without it there is no denominator to speak of.
Synthea does track enrolment -- in `payer_transitions.csv`, in the CSV export.

So `coverage_from_payer_transitions` BUILDS Coverage resources from that file.
Those resources are DERIVED, not Synthea's, and they are marked as such in
`meta.tag` so nobody downstream mistakes them for source data. This is the kind
of thing that quietly becomes folklore if it is not labelled at the point of
manufacture.

SEAM 3 -- THE VOCABULARY IS DIFFERENT
This project's value sets were hand-built with illustrative codes. Synthea
speaks SNOMED for conditions, LOINC for observations, CVX for immunisations. A
value set that lists ICD-10 codes matches nothing in a SNOMED-coded record --
again silently, again reporting zero.

`discover_codes` therefore MEASURES what is actually in the data rather than
assuming, and `SYNTHEA_VALUE_SETS` maps only codes that were observed. Guessing
the codes would reproduce the original mistake in the other direction.
"""

from __future__ import annotations

import csv
import glob
import json
import os

# Codes that Synthea actually emits, confirmed by `discover_codes` against
# generated output rather than taken from a specification. Anything not
# observed is absent rather than guessed.
SYNTHEA_VALUE_SETS = {
    # Diabetes. `discover_codes` found four candidates; three are included and
    # one is DELIBERATELY EXCLUDED.
    #
    # 714628002 "Prediabetes" is the most common of the four (257 occurrences
    # against 57 for type 2 diabetes itself) and it is NOT diabetes. Including
    # it would have MORE THAN QUADRUPLED the denominator with people who do not
    # have the condition the measure is about -- and the resulting rate would
    # have looked entirely plausible.
    "Diabetes": [
        ("http://snomed.info/sct", "44054006",
         "Diabetes mellitus type 2 (disorder)"),
        ("http://snomed.info/sct", "127013003",
         "Disorder of kidney due to diabetes mellitus"),
        ("http://snomed.info/sct", "90781000119102",
         "Microalbuminuria due to type 2 diabetes mellitus"),
    ],
    "HbA1c Laboratory Test": [
        ("http://loinc.org", "4548-4", "Hemoglobin A1c/Hemoglobin.total"),
    ],
    # Three distinct mammography codes appear. A value set carrying only the
    # commonest would miss 5 of 118 procedures -- small here, and exactly the
    # kind of quiet under-count that makes a numerator look worse than it is.
    "Mammography": [
        ("http://snomed.info/sct", "71651007", "Mammography (procedure)"),
        ("http://snomed.info/sct", "24623002", "Screening mammography"),
        ("http://snomed.info/sct", "241055006", "Mammogram - symptomatic"),
    ],
    # NOT PRESENT IN SYNTHEA. Left empty rather than populated from a
    # specification, because an empty value set makes the BCS exclusion
    # visibly untestable on this data, whereas a plausible-looking code that
    # matches nothing looks like it works.
    "Bilateral Mastectomy": [],
    # The guess here was WRONG. 183452005 was assumed; the code Synthea
    # actually emits is 305336008. An assumed code matches nothing, silently,
    # and the exclusion then removes nobody.
    "Hospice Encounter": [
        ("http://snomed.info/sct", "305336008", "Admission to hospice"),
    ],
    "DTaP Vaccine": [
        ("http://hl7.org/fhir/sid/cvx", "20", "DTaP"),
    ],
}


DERIVED_TAG = {
    "system": "urn:healthcare-hm:provenance",
    "code": "derived",
    "display": ("Constructed from Synthea payer_transitions.csv; NOT emitted "
                "by Synthea's FHIR exporter"),
}


def bundle_paths(synthea_dir):
    return sorted(glob.glob(os.path.join(synthea_dir, "fhir", "*.json")))


def _is_patient_bundle(path):
    base = os.path.basename(path)
    return not (base.startswith("hospitalInformation")
                or base.startswith("practitionerInformation"))


def coverage_from_payer_transitions(synthea_dir):
    """{patient_id: [Coverage, ...]} derived from the CSV export.

    Synthea's FHIR export has no Coverage resource, and continuous enrolment
    gates every denominator. The spans exist in `payer_transitions.csv`, so
    they are lifted from there and marked `meta.tag` = derived, because a
    resource this pipeline manufactured must never be mistaken for one the
    generator produced.
    """
    path = os.path.join(synthea_dir, "csv", "payer_transitions.csv")
    if not os.path.exists(path):
        return {}

    out = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            pid = row.get("PATIENT")
            start, end = row.get("START_DATE"), row.get("END_DATE")
            if not (pid and start and end):
                continue
            out.setdefault(pid, []).append({
                "resourceType": "Coverage",
                "id": "cov-%s-%d" % (pid[:8], len(out.get(pid, []))),
                "meta": {"tag": [DERIVED_TAG]},
                "status": "active",
                "beneficiary": {"reference": "urn:uuid:%s" % pid},
                # payor is REQUIRED in R4 (1..*); a display-only Reference is
                # honest here because no Organization is carried across
                "payor": [{"display": row.get("PAYER") or "unknown payer"}],
                "period": {"start": start[:10], "end": end[:10]},
            })
    return out


def to_ndjson(synthea_dir, out_path, with_coverage=True):
    """Write Synthea bundles as NDJSON the warehouse loader can read.

    Returns a dict of counts, including how many Coverage resources had to be
    manufactured -- a number worth surfacing rather than burying.
    """
    coverage = coverage_from_payer_transitions(synthea_dir) if with_coverage \
        else {}

    n_bundles = n_entries = n_cov = n_patients = 0
    with open(out_path, "w", encoding="utf-8") as out:
        for path in bundle_paths(synthea_dir):
            if not _is_patient_bundle(path):
                continue
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)

            pid = None
            for entry in bundle.get("entry", []):
                if entry.get("resource", {}).get("resourceType") == "Patient":
                    pid = entry["resource"]["id"]
                    break
            if pid is None:
                continue
            n_patients += 1

            for cov in coverage.get(pid, []):
                bundle.setdefault("entry", []).append({"resource": cov})
                n_cov += 1

            n_entries += len(bundle.get("entry", []))
            n_bundles += 1
            out.write(json.dumps(bundle) + "\n")

    return {"bundles": n_bundles, "patients": n_patients,
            "entries": n_entries, "coverage_derived": n_cov,
            "path": out_path}


def discover_codes(synthea_dir, limit_files=None):
    """What codes are ACTUALLY in this data, by resource type.

    Written because the alternative -- looking up what Synthea "should" emit --
    is how the original value sets came to list codes that matched nothing. The
    data is the authority on what the data contains.
    """
    import collections

    found = collections.defaultdict(collections.Counter)
    paths = [p for p in bundle_paths(synthea_dir) if _is_patient_bundle(p)]
    if limit_files:
        paths = paths[:limit_files]

    field = {"Condition": "code", "Observation": "code",
             "Procedure": "code", "Immunization": "vaccineCode",
             "Encounter": "type"}

    for path in paths:
        with open(path, encoding="utf-8") as fh:
            bundle = json.load(fh)
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            rtype = res.get("resourceType")
            key = field.get(rtype)
            if not key:
                continue
            concept = res.get(key)
            if isinstance(concept, list):
                concept = concept[0] if concept else None
            if not concept:
                continue
            for coding in (concept.get("coding") or []):
                found[rtype][(coding.get("system"), coding.get("code"),
                              coding.get("display"))] += 1
    return found
