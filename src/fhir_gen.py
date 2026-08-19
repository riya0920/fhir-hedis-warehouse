"""Generate FHIR R4 bundles, including the edge cases that break measure logic.

Synthea is the right source and is named in the spec. It is a Java application
and is not runnable offline here, so bundles are emitted directly. The loss is
real -- Synthea's clinical trajectories come from curated disease modules -- and
the gain is that five specific edge cases are PLANTED with known expected
answers, so measure correctness is checkable rather than plausible.

THE FIVE PLANTED EDGE CASES
---------------------------
Measure logic IS edge-case logic. These are the ones that separate a measure
from a count, and each has a known correct answer recorded in EDGE_CASES:

  EC1  mid-year enrollee      -- enrolled from July. Fails continuous
                                 enrolment, so NOT in the denominator at all.
                                 The trap: they have a qualifying HbA1c, so a
                                 naive numerator-first implementation counts
                                 them and inflates the rate.
  EC2  allowable gap          -- one 30-day gap. Continuous enrolment permits
                                 one gap of up to 45 days, so this member IS
                                 in the denominator. The mirror trap: an
                                 implementation requiring unbroken coverage
                                 drops them and deflates the denominator.
  EC3  numerator event 1 day late -- HbA1c on 1 Jan of the following year.
                                 NOT in the numerator. The spec's period
                                 boundary decides, not intuition.
  EC4  exclusion-qualifying   -- diabetic with a hospice encounter. Removed
                                 from the denominator entirely, even though
                                 they have a qualifying test.
  EC5  age boundary           -- turns 76 during the measurement year. The
                                 CDC-style measure is 18-75 as of the END of
                                 the year, so they age out and are excluded.

Every one of these is a member who "obviously has diabetes and obviously had a
test" and is nonetheless not a numerator hit. That is the gap between a count
and a measure.
"""

from __future__ import annotations

import json
import os
import random
from datetime import date, timedelta

MY_START, MY_END = date(2024, 1, 1), date(2024, 12, 31)

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
CVX = "http://hl7.org/fhir/sid/cvx"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
CPT = "http://www.ama-assn.org/go/cpt"

# concept -> (system, code, display)
CODES = {
    "dm_type2": (SNOMED, "44054006", "Diabetes mellitus type 2"),
    "dm_type1": (SNOMED, "46635009", "Diabetes mellitus type 1"),
    "dm_icd": (ICD10, "E11.9", "Type 2 diabetes mellitus without complications"),
    "hba1c": (LOINC, "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "hba1c_alt": (LOINC, "17856-6", "Hemoglobin A1c in Blood by HPLC"),
    "mammogram": (CPT, "77067", "Screening mammography, bilateral"),
    "mammogram_sno": (SNOMED, "24623002", "Screening mammography"),
    "mastectomy_bilateral": (SNOMED, "428251008", "History of bilateral mastectomy"),
    "hospice": (SNOMED, "170935008", "Hospice care"),
    "dtap": (CVX, "20", "DTaP"),
    "esrd": (SNOMED, "46177005", "End stage renal disease"),
}

EDGE_CASES = {
    "EC1-midyear": {"expected_denominator": False, "expected_numerator": False,
                    "why": "enrolled from July; fails continuous enrolment"},
    "EC2-allowable-gap": {"expected_denominator": True, "expected_numerator": True,
                          "why": "one 30-day gap is within the 45-day allowance"},
    "EC3-late-event": {"expected_denominator": True, "expected_numerator": False,
                       "why": "HbA1c is 1 day after the measurement period"},
    "EC4-hospice": {"expected_denominator": False, "expected_numerator": False,
                    "why": "hospice is a required exclusion"},
    "EC5-age-out": {"expected_denominator": False, "expected_numerator": False,
                    "why": "turns 76 during the year; age is taken as of 31 Dec"},
}


def _cc(concept, text=None):
    system, code, display = CODES[concept]
    return {"coding": [{"system": system, "code": code, "display": display}],
            "text": text or display}


def _patient(pid, birth, gender):
    return {"resourceType": "Patient", "id": pid,
            "identifier": [{"system": "urn:oid:2.16.840.1.113883.19.5",
                            "value": f"MBR{pid}"}],
            "birthDate": birth.isoformat(), "gender": gender}


def _coverage(pid, spans):
    return [{"resourceType": "Coverage", "id": f"cov-{pid}-{i}",
             "status": "active", "beneficiary": {"reference": f"Patient/{pid}"},
             "period": {"start": a.isoformat(), "end": b.isoformat()}}
            for i, (a, b) in enumerate(spans)]


def _condition(pid, concept, onset, i):
    return {"resourceType": "Condition", "id": f"cond-{pid}-{i}",
            "subject": {"reference": f"Patient/{pid}"},
            "clinicalStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": "active"}]},
            "verificationStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                "code": "confirmed"}]},
            "code": _cc(concept), "onsetDateTime": onset.isoformat()}


def _observation(pid, concept, when, value, i):
    return {"resourceType": "Observation", "id": f"obs-{pid}-{i}",
            "status": "final", "subject": {"reference": f"Patient/{pid}"},
            "code": _cc(concept), "effectiveDateTime": when.isoformat(),
            "valueQuantity": {"value": value, "unit": "%",
                              "system": "http://unitsofmeasure.org", "code": "%"}}


def _procedure(pid, concept, when, i):
    return {"resourceType": "Procedure", "id": f"proc-{pid}-{i}",
            "status": "completed", "subject": {"reference": f"Patient/{pid}"},
            "code": _cc(concept), "performedDateTime": when.isoformat()}


def _encounter(pid, concept, when, i):
    return {"resourceType": "Encounter", "id": f"enc-{pid}-{i}",
            "status": "finished", "subject": {"reference": f"Patient/{pid}"},
            "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                      "code": "AMB"},
            "type": [_cc(concept)],
            "period": {"start": when.isoformat(), "end": when.isoformat()}}


def _immunization(pid, concept, when, i):
    return {"resourceType": "Immunization", "id": f"imm-{pid}-{i}",
            "status": "completed", "patient": {"reference": f"Patient/{pid}"},
            "vaccineCode": _cc(concept), "occurrenceDateTime": when.isoformat()}


def _bundle(pid, resources):
    return {"resourceType": "Bundle", "id": f"bundle-{pid}", "type": "collection",
            "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r}
                      for r in resources]}


def _rand_date(rng, a, b):
    return a + timedelta(days=rng.randrange((b - a).days + 1))


def build_patient(rng, pid, forced=None):
    """One patient's bundle. `forced` builds a planted edge case."""
    if forced == "EC1-midyear":
        birth = date(1970, 3, 4)
        spans = [(date(2024, 7, 1), MY_END)]
        res = [_patient(pid, birth, "female"), *_coverage(pid, spans),
               _condition(pid, "dm_type2", date(2019, 5, 1), 0),
               _observation(pid, "hba1c", date(2024, 9, 12), 7.2, 0)]
        return _bundle(pid, res)
    if forced == "EC2-allowable-gap":
        birth = date(1968, 2, 2)
        spans = [(MY_START, date(2024, 4, 30)), (date(2024, 5, 31), MY_END)]
        res = [_patient(pid, birth, "male"), *_coverage(pid, spans),
               _condition(pid, "dm_type2", date(2018, 1, 1), 0),
               _observation(pid, "hba1c", date(2024, 8, 3), 6.8, 0)]
        return _bundle(pid, res)
    if forced == "EC3-late-event":
        birth = date(1975, 6, 6)
        res = [_patient(pid, birth, "female"), *_coverage(pid, [(MY_START, MY_END)]),
               _condition(pid, "dm_type2", date(2017, 1, 1), 0),
               _observation(pid, "hba1c", date(2025, 1, 1), 7.0, 0)]
        return _bundle(pid, res)
    if forced == "EC4-hospice":
        birth = date(1960, 9, 9)
        res = [_patient(pid, birth, "male"), *_coverage(pid, [(MY_START, MY_END)]),
               _condition(pid, "dm_type2", date(2015, 1, 1), 0),
               _observation(pid, "hba1c", date(2024, 6, 1), 8.1, 0),
               _encounter(pid, "hospice", date(2024, 3, 15), 0)]
        return _bundle(pid, res)
    if forced == "EC5-age-out":
        birth = date(1948, 5, 20)          # turns 76 in 2024
        res = [_patient(pid, birth, "female"), *_coverage(pid, [(MY_START, MY_END)]),
               _condition(pid, "dm_type2", date(2010, 1, 1), 0),
               _observation(pid, "hba1c", date(2024, 4, 4), 7.5, 0)]
        return _bundle(pid, res)

    # ---- ordinary patient ----------------------------------------------
    age = rng.randint(2, 84)
    birth = date(2024 - age, rng.randint(1, 12), rng.randint(1, 28))
    gender = rng.choice(["female", "male"])

    r = rng.random()
    if r < 0.70:
        spans = [(MY_START, MY_END)]
    elif r < 0.80:                                    # allowable gap
        g = _rand_date(rng, date(2024, 3, 1), date(2024, 9, 1))
        spans = [(MY_START, g), (g + timedelta(days=rng.randint(5, 44)), MY_END)]
    elif r < 0.90:                                    # disqualifying gap
        g = _rand_date(rng, date(2024, 3, 1), date(2024, 8, 1))
        spans = [(MY_START, g), (g + timedelta(days=rng.randint(60, 150)), MY_END)]
    else:                                             # partial year
        spans = [(_rand_date(rng, date(2024, 2, 1), date(2024, 10, 1)), MY_END)]

    res = [_patient(pid, birth, gender), *_coverage(pid, spans)]
    i = 0

    diabetic = age >= 18 and rng.random() < 0.13
    if diabetic:
        concept = "dm_type1" if rng.random() < 0.08 else "dm_type2"
        res.append(_condition(pid, concept, _rand_date(
            rng, date(2010, 1, 1), date(2023, 12, 31)), i))
        i += 1
        if rng.random() < 0.05:                       # coded in ICD-10 instead
            res.append(_condition(pid, "dm_icd", date(2022, 6, 1), i))
            i += 1
        if rng.random() < 0.78:                       # numerator: HbA1c in MY
            code = "hba1c" if rng.random() < 0.85 else "hba1c_alt"
            res.append(_observation(pid, code, _rand_date(rng, MY_START, MY_END),
                                    round(rng.uniform(5.4, 11.2), 1), i))
            i += 1
        elif rng.random() < 0.25:                     # test, but out of period
            res.append(_observation(pid, "hba1c", _rand_date(
                rng, date(2023, 1, 1), date(2023, 12, 31)),
                round(rng.uniform(5.4, 11.2), 1), i))
            i += 1

    if gender == "female" and 50 <= age <= 74:
        if rng.random() < 0.03:
            res.append(_condition(pid, "mastectomy_bilateral",
                                  date(2021, 4, 1), i))
            i += 1
        elif rng.random() < 0.71:
            concept = "mammogram" if rng.random() < 0.8 else "mammogram_sno"
            res.append(_procedure(pid, concept, _rand_date(
                rng, date(2022, 10, 1), MY_END), i))
            i += 1

    if age == 2:
        for k in range(rng.choice([0, 2, 3, 4, 4, 4, 5])):
            res.append(_immunization(pid, "dtap", date(
                2022 + (k // 4), rng.randint(1, 12), rng.randint(1, 28)), i))
            i += 1

    if rng.random() < 0.012:
        res.append(_encounter(pid, "hospice", _rand_date(rng, MY_START, MY_END), i))
        i += 1
    if rng.random() < 0.02:
        res.append(_condition(pid, "esrd", date(2022, 1, 1), i))
        i += 1

    return _bundle(pid, res)


def generate(n=20000, seed=23, outdir="data"):
    """Write bundles as NDJSON -- one bundle per line.

    Deviation from the spec, stated: Synthea writes one JSON file per patient.
    20,000 small files is a filesystem problem rather than a data problem, so
    they are written as newline-delimited JSON. This is also what a real
    ingestion path looks like (FHIR bulk export, $export, produces NDJSON).
    """
    os.makedirs(outdir, exist_ok=True)
    rng = random.Random(seed)
    path = f"{outdir}/bundles.ndjson"
    with open(path, "w", encoding="utf-8") as fh:
        for name in EDGE_CASES:
            fh.write(json.dumps(build_patient(rng, name, forced=name)) + "\n")
        for i in range(n):
            fh.write(json.dumps(build_patient(rng, f"P{i:06d}")) + "\n")
    print(f"wrote {path}: {n:,} patients + {len(EDGE_CASES)} planted edge cases")
    return path


if __name__ == "__main__":
    generate()
