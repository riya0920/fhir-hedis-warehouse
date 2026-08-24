"""An INDEPENDENT re-implementation of the measures, straight from the bundles.

WHY THIS FILE EXISTS
--------------------
The spec asks for the measure to be hand-computed for a 25-patient sample and
reconciled to the pipeline 100%. Hand-computing 25 patients across 3 measures is
75 manual determinations, which is not verifiable in a repository and not
repeatable when the generator seed changes.

So: a SECOND implementation, deliberately built along a different path. It
reads the FHIR bundles directly with no warehouse, no SQL, no value-set tables,
no shared helper functions, and hard-coded code lists rather than seeded value
sets. Two independent paths from the same source to the same answer.

This is a weaker check than a human reading a chart -- both implementations
share my misunderstandings of the specification, so a conceptual error in the
measure definition reproduces identically in both and reconciles perfectly. It
catches implementation bugs, not specification misreadings, and that limit is
worth being explicit about rather than letting "reconciled 100%" imply more
than it does.

What it does catch, and what would otherwise be invisible: a value-set seed row
with a typo, a join that drops rows, a date comparison on strings that works
until a year boundary, a set operation that silently deduplicates.
"""

from __future__ import annotations

import json
from datetime import date

MY_START, MY_END = date(2024, 1, 1), date(2024, 12, 31)

# Hard-coded on purpose: the pipeline reads these from the value_set TABLE, so
# a typo in a seed row is a difference between the two paths rather than a
# shared assumption.
DM_CODES = {("http://snomed.info/sct", "44054006"),
            ("http://snomed.info/sct", "46635009"),
            ("http://hl7.org/fhir/sid/icd-10-cm", "E11.9"),
            ("http://hl7.org/fhir/sid/icd-10-cm", "E10.9")}
A1C_CODES = {("http://loinc.org", "4548-4"), ("http://loinc.org", "17856-6"),
             ("http://loinc.org", "4549-2")}
MAM_CODES = {("http://www.ama-assn.org/go/cpt", "77067"),
             ("http://www.ama-assn.org/go/cpt", "77066"),
             ("http://snomed.info/sct", "24623002")}
# NOT SNOMED 428251008 -- that code means "History of appendectomy". See
# src/fhir_gen.py for the full account of the bug and why no replacement
# SNOMED code is guessed here.
MASTECTOMY = {("urn:healthcare-hm:example-codes", "EXAMPLE-BILAT-MASTECTOMY")}
HOSPICE = {("http://snomed.info/sct", "170935008")}
DTAP = {("http://hl7.org/fhir/sid/cvx", "20"),
        ("http://hl7.org/fhir/sid/cvx", "106")}


def _iter_bundles(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            yield json.loads(line)


def _d(s):
    return date.fromisoformat(s[:10])


def _codes(resource, field):
    cc = resource.get(field) or {}
    if isinstance(cc, list):
        cc = cc[0] if cc else {}
    return {(c.get("system"), c.get("code")) for c in (cc.get("coding") or [])}


def _age(birth, when):
    b = _d(birth)
    return when.year - b.year - ((when.month, when.day) < (b.month, b.day))


def _continuous(periods):
    """Independently written: sort, walk, measure gaps. Same rule, different code."""
    spans = []
    for p in periods:
        a, b = _d(p["start"]), _d(p["end"])
        a, b = max(a, MY_START), min(b, MY_END)
        if a <= b:
            spans.append((a, b))
    if not spans:
        return False
    spans.sort()
    gap_total = []
    if spans[0][0] > MY_START:
        gap_total.append((spans[0][0] - MY_START).days)
    for i in range(1, len(spans)):
        delta = (spans[i][0] - spans[i - 1][1]).days - 1
        if delta > 0:
            gap_total.append(delta)
    if spans[-1][1] < MY_END:
        gap_total.append((MY_END - spans[-1][1]).days)
    return len(gap_total) <= 1 and (max(gap_total) if gap_total else 0) <= 45


def evaluate(bundle):
    """Return {measure: (in_denominator, in_numerator)} for one bundle."""
    patient = None
    periods, conditions, observations, procedures, encounters, imms = [], [], [], [], [], []
    for e in bundle.get("entry", []):
        r = e["resource"]
        t = r["resourceType"]
        if t == "Patient":
            patient = r
        elif t == "Coverage":
            periods.append(r["period"])
        elif t == "Condition":
            conditions.append(r)
        elif t == "Observation":
            observations.append(r)
        elif t == "Procedure":
            procedures.append(r)
        elif t == "Encounter":
            encounters.append(r)
        elif t == "Immunization":
            imms.append(r)
    if patient is None:
        return {}

    age = _age(patient["birthDate"], MY_END)
    enrolled = _continuous(periods)
    hospice = any(_codes(e, "type") & HOSPICE for e in encounters)

    out = {}

    # CDC-A1C
    diabetic = any(_codes(c, "code") & DM_CODES for c in conditions)
    in_d = diabetic and 18 <= age <= 75 and enrolled and not hospice
    in_n = in_d and any(
        (_codes(o, "code") & A1C_CODES)
        and MY_START <= _d(o["effectiveDateTime"]) <= MY_END
        for o in observations)
    out["CDC-A1C"] = (in_d, in_n)

    # BCS
    mastectomy = any(_codes(c, "code") & MASTECTOMY for c in conditions)
    in_d = (patient.get("gender") == "female" and 50 <= age <= 74
            and enrolled and not mastectomy and not hospice)
    lookback = date(2022, 10, 1)
    in_n = in_d and any(
        (_codes(p, "code") & MAM_CODES)
        and lookback <= _d(p["performedDateTime"]) <= MY_END
        for p in procedures)
    out["BCS"] = (in_d, in_n)

    # CIS-DTaP
    in_d = age == 2 and enrolled
    in_n = in_d and sum(1 for im in imms if _codes(im, "vaccineCode") & DTAP) >= 4
    out["CIS-DTaP"] = (in_d, in_n)
    return out


def sample_patient_ids(path, n=25, seed=99):
    import random
    rng = random.Random(seed)
    ids = [b["entry"][0]["resource"]["id"] for b in _iter_bundles(path)
           if b.get("entry")]
    return set(rng.sample(ids, min(n, len(ids))))


def reconcile(path, con, results, only_ids=None):
    """Compare the pipeline's membership sets against the reference."""
    out = {k: {"n": 0, "mismatches": 0, "detail": []} for k in results}
    for bundle in _iter_bundles(path):
        if not bundle.get("entry"):
            continue
        pid = bundle["entry"][0]["resource"]["id"]
        if only_ids is not None and pid not in only_ids:
            continue
        ref = evaluate(bundle)
        for key, r in results.items():
            exp_d, exp_n = ref.get(key, (False, False))
            got_d = pid in r.denominator_ids
            got_n = pid in r.numerator_ids
            out[key]["n"] += 1
            if (exp_d, exp_n) != (got_d, got_n):
                out[key]["mismatches"] += 1
                out[key]["detail"].append(
                    f"{pid}: reference denom={exp_d} num={exp_n}, "
                    f"pipeline denom={got_d} num={got_n}")
    return out
