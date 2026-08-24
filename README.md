# DATA-2 — FHIR to warehouse + HEDIS-style measures — working system, 10 known gaps

**The gap between a count and a measure is the entire job.** This builds the
measure: initial population → denominator → exclusions → numerator, every stage
counted, every code coming from a value set rather than a literal.

```bash
python run_warehouse.py    # generate 20K bundles -> warehouse -> measures -> reconcile
python run_incremental.py     # incremental load, late arrivals, restatement
python -m pytest tests -q     # 82 tests
python validate_fhir.py       # R4B schema validation -> docs/
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

## Incremental load, late arrivals, and a measure that had to be restated

`run_incremental.py`. The gap list said "no incremental load; full rebuild
every run; no CDC, no late-arriving data, no restatement handling". This is
those, and the result is a genuine restatement rather than a demonstration.

### The watermark is `meta.lastUpdated`, and getting that wrong fails silently

FHIR already specifies the mechanism: `$export?_since=<instant>` returns
resources whose `meta.lastUpdated` is at or after that instant. So the
watermark is the **receipt** clock, not the clinical one.

That distinction is the whole file. HEDIS is computed from clinical dates — was
the A1c drawn during the measurement year — while the pipeline is fed in
receipt order, and the two are unrelated. Key the load on a clinical date and a
January service received in November is **never loaded at all**: the watermark
passed January ten months earlier. No error, no gap in the row count, just a
rate that is quietly too low forever.

The generator now stamps `meta.lastUpdated` from a receipt-lag distribution
with a real tail (8% arrive 120–300 days late), because a generator without it
cannot exercise an incremental load at all.

### The restatement is real

Two runs of the *same code on the same definitions*:

| | 30 days runout (submitted) | 6 months runout | change |
|---|---|---|---|
| CDC-A1C | 956/1,370 = **0.6978** | 996/1,380 = **0.7217** | **+2.39 pp** |
| BCS | 1,492/2,300 = **0.6487** | 1,519/2,312 = **0.6570** | **+0.83 pp** |

109 resources arrived into the closed measurement year after it was reported —
median lag 227 days, max 299. The submission rate was 2.4 points low because
that is what 30 days of runout looks like.

**These are not errors, and a pipeline that silently overwrites the published
number cannot answer the only question an auditor asks**: what changed between
the submission and today. `measure_run` records every run with an `is_final`
flag, and `restatements()` compares against the last *final* run only —
comparing against every prior run would flag ordinary intra-period movement as
a restatement, which is not what the word means. Each one names its driver
(denominator grew / shrank / numerator only), because "more members entered the
measure" and "members already counted became compliant" are different
conversations.

### The migration case, made to fire

Every counter reported `0` for "returned but content-identical", because
nothing in the data had been touched without changing — and **a defence whose
counter has never moved has not been shown to work**. So the run simulates a
server re-index that bumps `meta.lastUpdated` on everything and changes nothing
else:

```
resources the `_since` export returns    51,227
classified as new                             0
classified as changed                         0
recognised as content-identical          51,227
late arrivals raised                          0
```

A pipeline keying on `lastUpdated` alone would treat all 51,227 as changes and
restate every measure it has ever published — from a re-index that changed no
clinical fact. `resource_version` stores a hash of the resource body **with
`meta` excluded**, which is the entire defence.

### What `_since` does not give you

Named because they are the reasons this is a demonstration and not a pipeline:

- **`meta.lastUpdated` moves on any write.** Handled, by content hashing.
- **Non-conformant servers do not always bump it.** Not handled — you are
  silently missing updates a `_since` export will never return again.
- **Deletes are not in a `_since` export at all.** A retracted resource is
  simply absent, and absence is indistinguishable from "not changed", so an
  append-only pipeline keeps counting a retracted A1c result forever.
- **No referential integrity across windows.** A Condition with a 2019 onset
  gets a 2019 `lastUpdated`, *earlier than its own Patient's*, so a window can
  return a resource referencing a Patient it never returned. That is a property
  of the specification, not of this generator — which is why the loader writes
  to tables with no foreign keys. A schema enforcing FKs would reject a
  legitimate export.

## SCD2 dimensions — why a disparity finding needs them

`src/scd2.py`. The gap list said: *"no snapshot/SCD2 history on dimensions, so
a patient's race recorded differently over time overwrites rather than versions,
and a stratified rate cannot be recomputed as it stood."*

The stratified HEDIS rates are computed by race and ethnicity, and a Patient
resource is **mutable**: a clerk corrects a field, a data-quality project
backfills self-reported race over an inferred value, a merge consolidates two
records. If the warehouse overwrites, **the disparity gap published in February
cannot be reproduced in June** — not because the measure changed, but because
the denominator's *attributes* changed underneath it.

The rate is recomputable. The stratification is not. And a disparity finding
that cannot be reproduced cannot be defended.

Each version carries `valid_from`, `valid_to` and `is_current`, so
`stratum_as_of(patients, "2025-01-30")` reconstructs February exactly.

**Two things that are easy to get wrong, and both have tests:**

- **A no-op update must not create a version.** `meta.lastUpdated` moves on any
  write, so without the content check a server re-index produces a new version
  per patient per migration, and the history becomes noise that hides the three
  real changes inside it — the same discipline `incremental.py` applies to
  facts, applied to dimensions.
- **`valid_to` is exclusive**, and the previous version's `valid_to` equals the
  next one's `valid_from`. An inclusive bound set to "the day before" breaks the
  moment two changes land on the same day, which is precisely when a
  data-quality project is running.

`drift()` reports the **movements**, not just a count: "3 patients moved between
race categories" and "3 moved from unknown to a recorded value" are different
events, and the second shrinks an unknown bucket that was suppressing the gap.

## A merge that applies a delta

The second named gap: *"`run_incremental.py` rebuilds a window rather than
applying a delta."* `merge_facts()` inserts what is new, updates what changed,
and **leaves unchanged rows untouched**.

That last part is not an optimisation. A merge that rewrites every row destroys
the one signal an operator has — *how much actually changed last night* — and
turns a 12-row delta into something indistinguishable from a corruption.

## The corpus is validated against the R4B schema

`src/fhir_gen.py` writes FHIR R4 bundles by hand. Every measure reads them and
every test asserts things about the resulting rates — so the corpus was checked
exhaustively for whether it says the **right things**, and never once for
whether it is **valid FHIR**.

Those are different questions, and the second had a bad answer.

| | valid | invalid |
|---|---|---|
| bundles | 65 | 0 |
| resources | 179 | 0 |

### What it found

**`Coverage.payor` is required in R4** (cardinality `1..*`) and the generator
omitted it. **All 82 Coverage resources were invalid FHIR.**

The measure logic never noticed, because continuous-enrolment only reads
`period`. A real FHIR server would have rejected every one of them on ingest.

That is the failure mode worth naming: the corpus was validated against **the
consumer it happened to have**, not against **the standard it claimed to
follow**. Seventy-seven passing tests could not see it, because none of them
were asking.

### The fix is a `display`, and the reason is not laziness

`payor` is now a Reference carrying only `display`. That is valid R4 and it is
the honest encoding: there is no `Organization` resource in this synthetic
corpus, so a `reference` pointing at one would be a dangling pointer dressed up
as provenance.

Adding an `Organization` would also have changed the **resource counts**, which
the incremental and migration tests measure directly — so the minimal correct
fix was also the one that keeps those tests meaningful. A test pins that.

### Mind the version

`fhir.resources` defaults to **R5**; this project targets R4, so the audit
imports the **R4B** models explicitly. Validating R4 resources against R5
produces failures that are version differences rather than bugs.

Schema validity is a **floor**, not conformance: no US Core profiles, no
`meta.profile`, no terminology-server validation, no HEDIS value-set
certification. A bundle can be structurally perfect and clinically nonsense.

## What is still missing, and why it cannot be closed here

- **No dbt models.** `dbt-core` **is installed** — an earlier version of this
  list said it was not, which was wrong. This is unbuilt, not blocked. The
  structure mirrors a dbt project
  but there is no `ref()` graph, no materialisation, no dbt tests as
  declarations, no docs site, no model contracts.
- **No real Synthea.** Bundles are emitted directly by `src/fhir_gen.py`, so
  the clinical trajectories are unearned.
- **No real value sets.** OIDs are illustrative placeholders and code members
  are hand-built subsets. VSAC needs a UMLS licence and a network; there is no
  version pinning and no inactivated-code handling.
- **Only 3 of ~90 HEDIS measures**, each simplified — no hybrid measures, no
  supplemental data, no measure-year versioning. The specifications are
  licensed and not available offline.
- **`_since` cannot see deletes**, and non-conformant servers do not always bump
  `meta.lastUpdated`. Both are properties of the specification and of the
  server, not of this code: a retracted resource is simply absent from an
  export, and absence is indistinguishable from "not changed".
- **No bitemporality.** SCD2 gives one time axis (when we believed it), not two
  (when it was true *and* when we believed it). Real clinical data wants both —
  a race correction applies retroactively to registration, not from the day the
  clerk fixed it — and separating them needs a second pair of columns and a
  query language that understands them.
- **Only race and ethnicity survive flattening.** Language, birth sex, gender
  identity and site-specific extensions are still dropped, so stratification is
  limited to two dimensions.
- **Only the first coding of each CodeableConcept survives**, so local EHR
  codes are lost.
- **No risk adjustment on the stratified rates.** A raw gap conflates the
  disparity with case-mix differences between strata, and separating them needs
  a risk model this build does not have.
- **SQLite, not a warehouse.** No partitioning, no clustering, no cost model,
  and the whole thing fits in memory.

## Files

| path | what |
|---|---|
| `src/fhir_gen.py` | FHIR R4 bundle generator + the 5 planted edge cases |
| `src/warehouse.py` | flattening, schema, value sets as seed data |
| `src/measures.py` | 3 measures, waterfalls, continuous enrolment, care gaps, stratification |
| `src/reference.py` | independent re-implementation for reconciliation |
| `run_warehouse.py` | ingest → measure → reconcile → verify → pin |
| `docs/FLATTENING.md` | what was dropped and what breaks later |
| `src/incremental.py` | `_since` watermark, content hashing, late arrivals, restatement |
| `run_incremental.py` | two runout windows, a real restatement, a simulated migration |
| `src/scd2.py` | dimension versioning, point-in-time strata, the fact merge |
| `validate_fhir.py` | R4B schema validation; found the missing Coverage.payor |
| `tests/test_fhir_validation.py` | 5 tests, incl. that the fix adds a field not a resource |
| `tests/test_scd2.py` | 15 tests: no-op updates, the exclusive bound, delta merges |
| `tests/test_incremental.py` | 22 tests: the watermark, the migration case, restatement |
| `tests/test_measures.py` | 40 tests, mostly boundaries |
