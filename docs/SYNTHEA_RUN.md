# Running the measures on Synthea

Every measure in this project was written against bundles this repository also
generates. That is a **closed loop** -- the generator emits the codes the value
sets look for, at the grain the loader expects, with the references the joins
assume. A pipeline can pass every test in that arrangement and still be unable
to read anybody else's data.

[Synthea](https://github.com/synthetichealth/synthea) is the reference
synthetic-data generator for this kind of work, it is free, and it is not
written by me. This is the data-level version of the discipline the rest of the
portfolio applies to code.

**Input:** 686 bundles, 984230 entries, of which **26367 Coverage resources were
derived** by this project rather than emitted by Synthea.

## Four seams, none of which raised an exception

**1. `urn:uuid:` references.** Synthea writes them throughout; `_ref_id` split
on `/` and returned the whole string, matching no `Patient.id`. Every clinical
resource would have joined to nobody and every denominator collapsed to zero --
and a measure reporting 0% reads as a finding, not a failure.

**2. No `Coverage` resource at all.** Synthea's FHIR export has none, and
continuous enrolment gates every HEDIS denominator. The spans exist in
`payer_transitions.csv`, so they are lifted from there and tagged
`meta.tag = derived`, because a resource this pipeline manufactured must never
be mistaken for one the generator produced.

**3. `performedPeriod`, not `performedDateTime`.** FHIR choice types let one
logical field be expressed either way. **100% of Synthea procedures** use the
half this loader did not read, so every procedure date was NULL and the BCS
numerator was empty -- until it crashed on a `None` comparison, which is the
only reason anyone noticed.

**4. A wrong SNOMED code that had been wrong all along.** Below.

## The value sets, measured against real data

| value set | rows matched, PROJECT codes | rows matched, OBSERVED codes |
|---|---|---|
| `Diabetes` | 57 | 195 |
| `HbA1c Laboratory Test` | 5860 | 5860 |
| `Mammography` | 3 | 118 |
| `Bilateral Mastectomy` | 0 | 0 |
| `Hospice Encounter` | 0 | 107 |
| `DTaP Vaccine` | 353 | 353 |

`Hospice Encounter` matched **zero** rows with the project's code. It is a
*required exclusion*, and it silently excluded nobody.

## What that does to the numbers

| measure | PROJECT value sets | OBSERVED codes |
|---|---|---|
| `CDC-A1C` | 42 / 46 = 91.30% | 51 / 67 = 76.12% |
| `BCS` | 1 / 101 = 0.99% | 4 / 87 = 4.60% |
| `CIS-DTaP` | 5 / 5 = 100.00% | 5 / 5 = 100.00% |

The CDC-A1C rate moves by **more than fifteen percentage points** on identical
patients. Nothing about the care changed -- only which codes the value set
recognised. This is what "the value sets are illustrative" actually costs, and
it is why that line in the gap list is not a footnote.

## The bug worth reading twice

The `Bilateral Mastectomy` value set contained **SNOMED 428251008**, labelled
*"History of bilateral mastectomy"*.

In SNOMED CT, 428251008 means **History of appendectomy.**

The generator emitted 428251008 and the value set looked for 428251008, so they
agreed perfectly and no test could see it. Synthea uses the code for its real
meaning. **28 appendectomy records matched the value set**, and of those
patients **4 were BCS-eligible women wrongly excluded** from breast-cancer
screening -- the denominator went from 97 to 101 once the code was corrected.
Four women taken out of the denominator means, in a real plan, four women nobody
ever contacts about a mammogram.

**The replacement code was not guessed.** Guessing is what caused the bug, and a
second plausible-looking wrong code would be worse than an obviously-local one.
The placeholder now uses `urn:healthcare-hm:example-codes`, which cannot be
mistaken for a terminology binding. The real code comes from VSAC and needs a
UMLS licence -- which is exactly the gap the list already names.

## What this does not show

Synthea is still synthetic, its epidemiology is module-driven, and its
enrolment spans are not a real plan's. Running on it proves the pipeline can
read data it did not author; it does not prove the measures are clinically
correct.
