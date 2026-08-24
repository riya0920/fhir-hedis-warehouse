# FHIR R4B validation of the generated corpus

`src/fhir_gen.py` writes FHIR R4 bundles by hand. Every measure reads them and
every test asserts things about the resulting rates -- so the corpus was checked
exhaustively for whether it says the right things, and never once for whether
it is **valid FHIR**.

Those are different questions, and the second had a bad answer.

## Result

| | valid | invalid |
|---|---|---|
| bundles | 65 | 0 |
| resources | **179** | **0** |

- `Condition` — 14 valid, 0 invalid
- `Coverage` — 82 valid, 0 invalid
- `Encounter` — 1 valid, 0 invalid
- `Observation` — 10 valid, 0 invalid
- `Patient` — 65 valid, 0 invalid
- `Procedure` — 7 valid, 0 invalid

## What it found

`Coverage.payor` is **required** in R4 (cardinality `1..*`) and the generator
omitted it entirely. **All 82 Coverage resources in the corpus were invalid
FHIR.**

The measure logic never noticed, because continuous-enrolment only reads
`period`. A real FHIR server would have rejected every one of them on ingest.

That is the failure mode worth naming: the corpus was validated against **the
consumer it happened to have**, not against **the standard it claimed to
follow**. Seventy-seven passing tests could not see it, because none of them
were asking.

### The fix, and why it is a `display`

`payor` is now a Reference carrying only `display`. That is valid R4, and it is
the honest encoding: there is no `Organization` resource in this synthetic
corpus, so a `reference` pointing at one would be a dangling pointer dressed up
as provenance. `display` says *"this is who paid, and we cannot resolve them"*.

Adding an `Organization` resource instead would have changed the resource
counts, which the incremental and migration tests measure directly -- so the
minimal correct fix was also the one that keeps those tests meaningful.

## Mind the version

`fhir.resources` defaults to **R5**. This project targets R4, so the audit
imports the **R4B** models explicitly. Validating R4 resources against R5
models produces failures that are version differences rather than bugs.

## What this does not do

Schema validity is a **floor**. No US Core profiles, no `meta.profile`, no
terminology-server validation of the code systems, no HEDIS value-set
certification. A bundle can be structurally perfect and clinically nonsense.
