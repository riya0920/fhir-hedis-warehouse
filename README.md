# DATA-2 — FHIR to warehouse + HEDIS-style measures (first 20%)

**The gap between a count and a measure is the entire job.** This builds the
measure: initial population → denominator → exclusions → numerator, every stage
counted, every code coming from a value set rather than a literal.

```bash
python run_warehouse.py    # generate 20K bundles -> warehouse -> measures -> reconcile
python -m pytest tests -q  # 26 tests
```

Offline, ~7 seconds end to end. 20,005 patients, 3 measures, 5 planted edge cases.

---

## The four things worth reading

### 1. The population waterfall

The artefact quality directors and auditors actually live on. A rate can fall
because the numerator fell **or** because the denominator grew, and those have
completely different owners — only the waterfall distinguishes them.

```
CDC-A1C: HbA1c testing for members with diabetes (18-75)
  all patients in the warehouse                      20,005
  with a diabetes diagnosis (value set)               2,151  (-17,854)
  aged 18-75 as of 31 Dec                             1,871  (-280)
  continuously enrolled (<=1 gap of <=45d)            1,512  (-359)
  after required exclusions (hospice)                 1,485  (-27)
  numerator: HbA1c during the measurement year        1,154  (-331)
  RATE                                                77.7%   (1,154/1,485)
```

| measure | denominator | numerator | rate |
|---|---|---|---|
| CDC-A1C | 1,485 | 1,154 | **77.7%** |
| BCS (breast cancer screening) | 2,307 | 1,636 | **70.9%** |
| CIS-DTaP (4+ doses by age 2) | 186 | 106 | **57.0%** |

Note the 359 members dropped by continuous enrolment. Without that rule the
denominator includes members the plan had no opportunity to serve — someone who
enrolled on 20 December cannot reasonably be expected to have been screened.
**The rate without continuous enrolment is not a worse estimate of the same
thing; it is an estimate of a different thing.**

### 2. Five planted edge cases, all passing

Measure logic *is* edge-case logic. Each of these is a member who "obviously has
diabetes and obviously had a test" and is nonetheless not a numerator hit.

| case | in denominator | in numerator | why |
|---|---|---|---|
| `EC1-midyear` | ✗ | ✗ | enrolled from July; fails continuous enrolment |
| `EC2-allowable-gap` | ✓ | ✓ | one 30-day gap is **within** the 45-day allowance |
| `EC3-late-event` | ✓ | ✗ | HbA1c is 1 day after the period ends |
| `EC4-hospice` | ✗ | ✗ | hospice is a required exclusion |
| `EC5-age-out` | ✗ | ✗ | turns 76 during the year; age is as of 31 Dec |

EC1 and EC2 are mirror traps. A numerator-first implementation counts EC1 and
inflates the rate; an implementation requiring unbroken coverage drops EC2 and
deflates the denominator. Both look like working code.

EC3 is the one to defend under pressure: *"the test was 2 days late — surely
that counts?"* The specification decides period boundaries, not intuition, and
the reason is that the alternative has no stopping point. If 1 day is fine, 3
are; and a measure whose boundary is negotiable is not comparable across plans,
which is the only thing a quality measure is for. The counter-argument — that
the patient plainly received appropriate care — is real, and it is an argument
for changing the specification, not for implementing it differently.

Boundaries are also unit-tested directly: a 45-day gap is allowed, a 46-day gap
is not.

### 3. Reconciliation against an independent implementation

`src/reference.py` re-implements all three measures reading the FHIR bundles
**directly** — no warehouse, no SQL, no value-set tables, no shared helper
functions, and hard-coded code lists rather than seeds.

```
25-patient sample: 0 mismatches
ALL patients:      0 mismatches across 60,015 comparisons
```

Two honest points. This is what "hand-computed for 25 patients" means when done
at repository scale; it is **not** a claim that I computed anything by hand. And
it is a weaker check than a human reading a chart: both implementations share my
reading of the specification, so a *conceptual* misunderstanding reproduces
identically in both and reconciles perfectly. It catches implementation bugs —
a typo in a seed row, a join that drops rows, a string date comparison that
works until a year boundary — not specification misreadings.

### 4. Value sets as data, and rates pinned as regression tests

Measures reference value sets by name; codes live in the `value_set` table.
`test_measures_reference_value_sets_not_inline_code_lists` fails if a code
literal ever appears in `measures.py`.

The reason is not tidiness. Value sets change — a new LOINC for the same assay,
an ICD-10 revision, a corrected publication — and a measure with inline codes
keeps using last year's definition until someone notices. Usually the auditor.

Rates are pinned in `out/measure_pins.json`. When a rate moves, the run says so
loudly:

> A measure rate moving is not a test failure to be silenced. It means the
> population definition, a value set, or event capture changed, and someone must
> say which and why before the pin is updated.

That is measure governance in miniature — and it is the answer to *"the quality
director says our screening rate is 6 points below last vendor's."* Debug
sequence: **population definition first** (are we counting the same people?),
then value sets, then event capture, then the vendor's spec version. Rates
differ by *specification* before they differ by data.

### Plus: care-gap drill-through

331 non-compliant CDC-A1C members, each with the missing event named. Measures
exist to drive outreach, not to produce a number for a slide — the care-gap list
is what a quality team actually works.

---

## Scope note, stated plainly

These are **HEDIS-style, not HEDIS**. The real specifications are licensed NCQA
publications running to dozens of pages per measure, versioned by measurement
year, with required exclusions this build does not implement (frailty and
advanced illness, institutional SNP status, palliative care), real VSAC value
sets, and precise rules for supplemental data and hybrid chart review.

Nothing here is certified, nothing would pass an NCQA audit, and the rates above
demonstrate measure *logic* rather than being HEDIS rates.

## What is missing (the other 80%)

- **No dbt.** Not installed. The structure mirrors a dbt project but there is no
  `ref()` graph, no lineage, no incremental materialisation, no snapshots, no
  dbt tests as declarations, no docs site, no model contracts.
- **No real Synthea.** Bundles are emitted directly by `src/fhir_gen.py`, so the
  clinical trajectories are unearned.
- **No real value sets.** OIDs are illustrative placeholders; code members are
  hand-built subsets. No VSAC, no licence, no version pinning, no handling of
  inactivated codes.
- **Only 3 of ~90 HEDIS measures**, and each simplified — no hybrid measures, no
  supplemental data, no measure-year versioning.
- **No US Core extensions**, therefore **no stratified reporting**. Race and
  ethnicity are dropped at flattening, so disparity analysis — an explicit CMS
  direction of travel — is impossible on this warehouse. Named in
  [`docs/FLATTENING.md`](docs/FLATTENING.md) as the most consequential loss.
- **Only the first coding of each CodeableConcept survives**, so local EHR codes
  are lost.
- **No incremental load.** Full rebuild every run; no CDC, no late-arriving data,
  no restatement handling.
- **SQLite, not a warehouse.** No partitioning, no clustering, no cost model, and
  the whole thing fits in memory.

## Files

| path | what |
|---|---|
| `src/fhir_gen.py` | FHIR R4 bundle generator + the 5 planted edge cases |
| `src/warehouse.py` | flattening, schema, value sets as seed data |
| `src/measures.py` | 3 measures with waterfalls, continuous enrolment, care gaps |
| `src/reference.py` | independent re-implementation for reconciliation |
| `run_warehouse.py` | ingest → measure → reconcile → verify → pin |
| `docs/FLATTENING.md` | what was dropped and what breaks later |
| `tests/test_measures.py` | 26 tests, mostly boundaries |
