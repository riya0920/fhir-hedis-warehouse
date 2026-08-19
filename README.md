# DATA-2 — FHIR to warehouse + HEDIS-style measures (~50% build)

**The gap between a count and a measure is the entire job.** This builds the
measure: initial population → denominator → exclusions → numerator, every stage
counted, every code coming from a value set rather than a literal.

```bash
python run_warehouse.py    # generate 20K bundles -> warehouse -> measures -> reconcile
python -m pytest tests -q  # 40 tests
```

Offline, ~7 seconds end to end. 20,005 patients, 3 measures, 5 planted edge cases.

---

## The five things worth reading

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

### 5. Stratified reporting — the loss FLATTENING.md called most consequential

US Core race and ethnicity arrive as **extensions**, not core elements. The
first version of the flattener dropped every extension, so disparity analysis
was impossible on this warehouse — named in `docs/FLATTENING.md` as the most
consequential loss, and it was, because stratified quality reporting is an
explicit CMS direction of travel.

They are now preserved (race recorded for 86.1% of patients — missingness is
modelled, because in real data it is substantial and **not random**).

**BCS by race:**

| stratum | denominator | rate | 95% CI |
|---|---|---|---|
| White | 1,194 | 67.3% | 65–70% |
| Black or African American | 349 | **52.4%** | 47–58% |
| (not recorded) | 287 | 70.4% | 65–75% |
| Asian | 217 | 68.7% | 62–74% |
| American Indian or Alaska Native | 117 | 54.7% | 46–63% |

Largest gap **14.9pp**, intervals do **not** overlap. The generator plants an
18pp screening penalty for two race groups, so the report recovers a gap of the
right size and direction — which is the only reason to believe a disparity
report at all.

Compare with **CDC-A1C**, where the lowest stratum is American Indian or Alaska
Native at 61.1% against White 74.2% — a 13.1pp gap whose **intervals overlap**
at n=54. Same pipeline, two different verdicts, and the difference is sample
size. Reporting the second as a finding would be launching a programme on noise.

**Three things the table refuses to do, enforced in code:**

1. **Missingness is its own row**, never dropped and never folded into a
   residual "other". Dropping unrecorded patients assumes the missingness is
   random; it varies by site, by registration workflow, and by whether anyone
   asked. A report that silently excludes a seventh of its denominator describes
   a population that does not exist.
2. **Cells below 30 are suppressed.** A rate over 11 patients is not a rate, and
   publishing it risks identifying them.
3. **A rate gap is not proof of a care gap.** It is a starting question. The
   difference may be access, referral patterns, data capture, or the measure
   specification interacting with a population — and mistaking a data artefact
   for a disparity sends the intervention to the wrong place.

Intervals are **Wilson**, not the normal approximation, because strata are small
and rates sit near the ends where the normal approximation runs past 0% and 100%
— which is how a quality report ends up claiming a screening rate of 104%.

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
- **Only race and ethnicity survive flattening.** Every other extension is
  still dropped — language, birth sex, gender identity, and any site-specific
  extension — so stratification is limited to two dimensions.
- **No risk adjustment on the stratified rates.** A raw rate gap conflates the
  disparity with differences in case mix between strata, and separating them
  needs the kind of adjustment this build does not do.
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
| `src/measures.py` | 3 measures, waterfalls, continuous enrolment, care gaps, stratification |
| `src/reference.py` | independent re-implementation for reconciliation |
| `run_warehouse.py` | ingest → measure → reconcile → verify → pin |
| `docs/FLATTENING.md` | what was dropped and what breaks later |
| `tests/test_measures.py` | 40 tests, mostly boundaries |
