"""HEDIS-style quality measures, with the population waterfall as the artefact.

WHAT MAKES A MEASURE, RATHER THAN A COUNT
-----------------------------------------
"Count the patients with diabetes codes" is a count. A measure is:

    initial population -> denominator -> exclusions -> numerator

with every stage counted, and every stage defined by a specification rather
than by intuition. The waterfall is the artefact quality directors and auditors
actually live on, because it is the only thing that answers "why did the rate
move" -- a rate can fall because the numerator fell OR because the denominator
grew, and those have completely different owners.

SCOPE NOTE, STATED PLAINLY
--------------------------
These are **HEDIS-style**, not HEDIS. The real specifications are licensed
publications from NCQA, run to dozens of pages per measure, and are versioned
by measurement year. They carry required exclusions this build does not
implement (frailty and advanced illness, institutional SNP status, palliative
care), specific value sets identified by real VSAC OIDs, and precise rules for
supplemental data and hybrid chart review.

Nothing here is certified, nothing here would pass an NCQA audit, and any rate
below is a demonstration of measure LOGIC rather than a HEDIS rate. Claiming
otherwise would be the overclaim that ends a screen.

CONTINUOUS ENROLMENT
--------------------
The denominator requires continuous enrolment through the measurement year
with **at most one gap of at most 45 days**. This is the convention HEDIS uses,
and both halves matter:

  * without a continuous-enrolment requirement, the denominator includes
    members the plan had no opportunity to serve -- someone who enrolled on
    20 December cannot reasonably be expected to have had a screening, and
    counting them makes the plan look worse than it is;
  * requiring UNBROKEN coverage instead deletes every churning member, which
    both shrinks the denominator and biases it toward the stably-insured.

So the rate without continuous enrolment is not a worse estimate of the same
thing; it is an estimate of a different thing.
"""

from __future__ import annotations

from datetime import date, timedelta

MY_START, MY_END = date(2024, 1, 1), date(2024, 12, 31)
ALLOWABLE_GAP_DAYS = 45


def _d(s):
    return date.fromisoformat(s[:10]) if s else None


def age_as_of(birth, when):
    b = _d(birth)
    return when.year - b.year - ((when.month, when.day) < (b.month, b.day))


def continuous_enrolment(spans, start=MY_START, end=MY_END,
                         allowable_gap=ALLOWABLE_GAP_DAYS):
    """(is_continuous, n_gaps, longest_gap_days) for the measurement period."""
    windows = sorted((max(_d(a), start), min(_d(b), end))
                     for a, b in spans if _d(a) <= end and _d(b) >= start)
    if not windows:
        return False, 1, (end - start).days + 1
    gaps, cursor = [], start
    for a, b in windows:
        if a > cursor:
            gaps.append((a - cursor).days)
        cursor = max(cursor, b + timedelta(days=1))
    if cursor <= end:
        gaps.append((end - cursor).days + 1)
    longest = max(gaps) if gaps else 0
    return (len(gaps) <= 1 and longest <= allowable_gap), len(gaps), longest


# ---------------------------------------------------------------------------
def value_set_codes(con, name):
    rows = con.execute(
        "SELECT code_system, code FROM value_set WHERE value_set_name = ?",
        (name,)).fetchall()
    return {(s, c) for s, c in rows}


def _patients(con):
    out = {}
    q = ("SELECT patient_id, member_id, birth_date, gender, race_code, "
         "race_display, ethnicity_code, ethnicity_display FROM patient")
    for pid, member_id, birth, gender, rc, rd, ec, ed in con.execute(q):
        out[pid] = {"patient_id": pid, "member_id": member_id,
                    "birth_date": birth, "gender": gender, "spans": [],
                    "race_code": rc, "race": rd,
                    "ethnicity_code": ec, "ethnicity": ed}
    for pid, a, b in con.execute(
            "SELECT patient_id, span_start, span_end FROM coverage"):
        if pid in out:
            out[pid]["spans"].append((a, b))
    return out


def _codes_by_patient(con, table, date_col, value_sets):
    """{patient_id: [(date, code_system, code)]} restricted to a value set."""
    want = set()
    for vs in value_sets:
        want |= value_set_codes(con, vs)
    out = {}
    q = f"SELECT patient_id, {date_col}, code_system, code FROM {table}"
    for pid, when, system, code in con.execute(q):
        if (system, code) in want:
            out.setdefault(pid, []).append((when, system, code))
    return out


# ---------------------------------------------------------------------------
class MeasureResult:
    def __init__(self, key, title):
        self.key, self.title = key, title
        self.waterfall = []
        self.denominator_ids = set()
        self.numerator_ids = set()
        self.excluded_ids = set()
        self.detail = {}

    def step(self, label, n):
        self.waterfall.append((label, n))

    @property
    def rate(self):
        d = len(self.denominator_ids)
        return len(self.numerator_ids) / d if d else float("nan")

    def as_dict(self):
        return {"key": self.key, "title": self.title,
                "waterfall": self.waterfall,
                "denominator": len(self.denominator_ids),
                "numerator": len(self.numerator_ids),
                "exclusions": len(self.excluded_ids),
                "rate": self.rate}


def cdc_hba1c(con):
    """HbA1c testing for members 18-75 with diabetes. CDC-style.

    Age is taken as of 31 December of the measurement year, which is the
    convention and is the reason the age-out edge case behaves as it does.
    """
    m = MeasureResult("CDC-A1C", "HbA1c testing for members with diabetes (18-75)")
    pats = _patients(con)
    m.step("all patients in the warehouse", len(pats))

    dm = _codes_by_patient(con, "condition", "onset_date", ["Diabetes"])
    initial = {p for p in pats if p in dm}
    m.step("with a diabetes diagnosis (value set)", len(initial))

    aged = {p for p in initial if 18 <= age_as_of(pats[p]["birth_date"], MY_END) <= 75}
    m.step("aged 18-75 as of 31 Dec", len(aged))

    enrolled, gap_detail = set(), {}
    for p in aged:
        ok, ngaps, longest = continuous_enrolment(pats[p]["spans"])
        gap_detail[p] = (ngaps, longest)
        if ok:
            enrolled.add(p)
    m.step("continuously enrolled (<=1 gap of <=45d)", len(enrolled))

    hospice = _codes_by_patient(con, "encounter", "start_date", ["Hospice Encounter"])
    excluded = {p for p in enrolled if p in hospice}
    m.excluded_ids = excluded
    denominator = enrolled - excluded
    m.step("after required exclusions (hospice)", len(denominator))
    m.denominator_ids = denominator

    a1c = _codes_by_patient(con, "observation", "effective_date",
                            ["HbA1c Laboratory Test"])
    numerator = set()
    for p in denominator:
        for when, _s, _c in a1c.get(p, []):
            if MY_START <= _d(when) <= MY_END:
                numerator.add(p)
                break
    m.step("numerator: HbA1c during the measurement year", len(numerator))
    m.numerator_ids = numerator
    m.detail = {"gaps": gap_detail, "a1c": a1c, "hospice": hospice}
    return m


def bcs(con):
    """Breast cancer screening: women 50-74, mammogram in the 27 months
    ending on the last day of the measurement year.

    The 27-month lookback is not arbitrary and is the detail most often got
    wrong: screening is biennial, so a 12-month lookback would fail every woman
    correctly screened 18 months ago. The extra 3 months allow for scheduling
    slippage.
    """
    m = MeasureResult("BCS", "Breast cancer screening (women 50-74)")
    pats = _patients(con)
    m.step("all patients in the warehouse", len(pats))

    women = {p for p in pats if pats[p]["gender"] == "female"}
    m.step("female", len(women))
    aged = {p for p in women if 50 <= age_as_of(pats[p]["birth_date"], MY_END) <= 74}
    m.step("aged 50-74 as of 31 Dec", len(aged))

    enrolled = {p for p in aged if continuous_enrolment(pats[p]["spans"])[0]}
    m.step("continuously enrolled (<=1 gap of <=45d)", len(enrolled))

    mastectomy = _codes_by_patient(con, "condition", "onset_date",
                                   ["Bilateral Mastectomy"])
    hospice = _codes_by_patient(con, "encounter", "start_date", ["Hospice Encounter"])
    excluded = {p for p in enrolled if p in mastectomy or p in hospice}
    m.excluded_ids = excluded
    denominator = enrolled - excluded
    m.step("after exclusions (bilateral mastectomy, hospice)", len(denominator))
    m.denominator_ids = denominator

    lookback_start = date(MY_END.year - 2, 10, 1)   # 27 months back from 31 Dec
    mam = _codes_by_patient(con, "procedure", "performed_date", ["Mammography"])
    numerator = {p for p in denominator
                 if any(lookback_start <= _d(w) <= MY_END for w, _s, _c in mam.get(p, []))}
    m.step("numerator: mammogram in the 27-month lookback", len(numerator))
    m.numerator_ids = numerator
    m.detail = {"mammography": mam, "lookback_start": lookback_start.isoformat()}
    return m


def cis_dtap(con):
    """Childhood immunisation: children turning 2 in the year, >=4 DTaP doses."""
    m = MeasureResult("CIS-DTaP", "Childhood immunisation status: 4+ DTaP by age 2")
    pats = _patients(con)
    m.step("all patients in the warehouse", len(pats))

    turning_two = {p for p in pats if age_as_of(pats[p]["birth_date"], MY_END) == 2}
    m.step("turning 2 during the measurement year", len(turning_two))

    enrolled = {p for p in turning_two if continuous_enrolment(pats[p]["spans"])[0]}
    m.step("continuously enrolled (<=1 gap of <=45d)", len(enrolled))
    m.denominator_ids = enrolled
    m.step("after exclusions (none implemented)", len(enrolled))

    imm = _codes_by_patient(con, "immunization", "occurrence_date", ["DTaP Vaccine"])
    numerator = {p for p in enrolled if len(imm.get(p, [])) >= 4}
    m.step("numerator: 4 or more DTaP doses", len(numerator))
    m.numerator_ids = numerator
    m.detail = {"immunisations": imm}
    return m


MEASURES = {"CDC-A1C": cdc_hba1c, "BCS": bcs, "CIS-DTaP": cis_dtap}


# ---------------------------------------------------------------------------
def care_gaps(con, result, limit=None):
    """Non-compliant members with the reason. The operational output.

    Measures exist to drive outreach, not to produce a number for a slide. The
    care-gap list is what a quality team actually works: who is in the
    denominator, not in the numerator, and what event is missing.
    """
    pats = _patients(con)
    gaps = []
    for p in sorted(result.denominator_ids - result.numerator_ids):
        gaps.append({"patient_id": p, "member_id": pats[p]["member_id"],
                     "measure": result.key, "status": "non-compliant",
                     "missing": _missing_for(result)})
        if limit and len(gaps) >= limit:
            break
    return gaps


def _missing_for(result):
    return {"CDC-A1C": "no HbA1c result during the measurement year",
            "BCS": "no mammogram in the 27-month lookback",
            "CIS-DTaP": "fewer than 4 DTaP doses recorded"}[result.key]


# ---------------------------------------------------------------------------
# Stratified reporting
# ---------------------------------------------------------------------------
SMALL_CELL_THRESHOLD = 30


def wilson_interval(successes, n, z=1.96):
    """Wilson score interval for a proportion.

    Wilson rather than the normal approximation because strata are small and
    rates sit near the ends, where the normal approximation produces intervals
    extending below 0 or above 1 -- which is how a quality report ends up
    claiming a screening rate of 104%.
    """
    if n == 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def stratify(con, result, dimension="race"):
    """Measure rate by a demographic stratum.

    THE CAVEATS ARE PART OF THE OUTPUT, not a footnote:

    * MISSINGNESS IS A CATEGORY. Patients with no recorded race get their own
      row; they are never dropped and never folded into a residual "other".
      Dropping them assumes the missingness is random, and it is not -- it
      varies by site, by registration workflow, and by whether anyone asked.
      A stratified report that silently excludes a seventh of its denominator
      is describing a population that does not exist.
    * SMALL CELLS ARE SUPPRESSED. A rate over 11 patients is not a rate, and
      publishing it risks identifying them.
    * A RATE GAP IS NOT PROOF OF A CARE GAP. It is a starting question. The
      difference may be access, referral patterns, data capture, or the measure
      specification interacting with a population -- and mistaking a data
      artefact for a disparity misdirects the intervention.
    """
    pats = _patients(con)
    groups = {}
    for pid in result.denominator_ids:
        key = pats[pid].get(dimension) or "(not recorded)"
        g = groups.setdefault(key, {"denominator": 0, "numerator": 0})
        g["denominator"] += 1
        if pid in result.numerator_ids:
            g["numerator"] += 1

    rows = []
    for key, g in groups.items():
        d, n = g["denominator"], g["numerator"]
        rows.append({"stratum": key, "denominator": d, "numerator": n,
                     "rate": (n / d) if d else float("nan"),
                     "suppressed": d < SMALL_CELL_THRESHOLD,
                     "ci": wilson_interval(n, d)})
    return sorted(rows, key=lambda r: -r["denominator"])


def disparity_summary(rows, reference=None):
    """Largest gap against a reference stratum, and whether it is resolvable.

    Overlapping confidence intervals are reported rather than a p-value,
    because the question a quality director actually asks is "is this gap big
    enough to act on" -- and a gap whose interval spans zero is not one to
    launch a programme over.
    """
    usable = [r for r in rows if not r["suppressed"]
              and r["stratum"] != "(not recorded)"]
    if len(usable) < 2:
        return None
    ref = (next((r for r in usable if r["stratum"] == reference), None)
           or max(usable, key=lambda r: r["denominator"]))
    worst = min(usable, key=lambda r: r["rate"])
    overlap = not (worst["ci"][1] < ref["ci"][0] or ref["ci"][1] < worst["ci"][0])
    return {"reference": ref["stratum"], "reference_rate": ref["rate"],
            "lowest": worst["stratum"], "lowest_rate": worst["rate"],
            "gap": ref["rate"] - worst["rate"],
            "intervals_overlap": overlap, "distinguishable": not overlap,
            "n_strata_reported": len(usable),
            "n_strata_suppressed": sum(1 for r in rows if r["suppressed"])}
