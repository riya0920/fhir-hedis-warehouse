# Flattening decisions, and what they cost

FHIR is a graph of nested, extensible resources. A warehouse is tables.
Flattening is therefore lossy, and the useful document is not "here is the
schema" but **"here is what I dropped, and here is what breaks if someone
needs it later."**

## The one rule that is never bent

**A code is stored with its system, always.**

`44054006` is diabetes mellitus type 2 in SNOMED CT. In any other code system
it is a different thing or nothing at all. Every code column in this warehouse
is a `(code_system, code, display)` triple, both columns `NOT NULL`, and
`test_no_code_is_ever_stored_without_its_system` fails if one ever lands
without its system.

This is most of what separates a FHIR warehouse from `json_normalize(bundle)`.
The failure it prevents is not an error — it is a `WHERE code = '44054006'`
that silently matches a CPT code with the same digits, in a query nobody
re-reads because it returns plausible numbers.

## What is dropped

| dropped | why | what breaks later |
|---|---|---|
| **All codings after the first** in a CodeableConcept | one column set per concept | Real resources carry several codings — a SNOMED concept *and* the site's local EHR code. Dropping the rest loses the local code, which is the one a site's own analysts and their existing reports use. This is the most consequential loss here. |
| **All extensions** | no generic extension store | Includes US Core race, ethnicity, and birth sex. **No disparity analysis is possible on this warehouse at all** — and stratified quality reporting is an explicit CMS direction of travel, so this is a gap that will need fixing rather than a nicety. |
| **Narrative (`text.div`)** | not queryable | Loses the human-readable rendering, which is what a clinician reviewing an audit finding actually wants to see. |
| **Contained resources** | no container model | Any resource defined inline rather than referenced disappears entirely. Not present in this data; would be in a real feed. |
| **Provenance / meta** | no lineage table | Cannot answer "which source system asserted this, and when was it last updated". That question arrives the first time two systems disagree. |
| **`Reference.display`, absolute and conditional references** | references stored as bare ids | Works because this data uses only relative `Type/id` references. A real feed with absolute URLs or conditional references would silently produce orphan ids — which is why `test_references_resolve_to_known_patients` exists rather than being assumed. |
| **Period end on Encounter beyond the first type coding** | single type column | Multi-type encounters collapse to their first type. |
| **`Observation.component`** | no component table | Panel results (blood pressure systolic/diastolic) cannot be represented. Fine here, fatal for a vitals feed. |

## What is preserved, deliberately

- code + system + display for every coded element
- `clinicalStatus` and `verificationStatus` on Condition, so a refuted or
  entered-in-error condition can be distinguished from an active one — dropping
  these is how "no evidence of pneumonia" becomes a diabetic's problem list
- coverage periods as spans, not a boolean, because continuous-enrolment logic
  is span arithmetic and a flag cannot express it
- `valueQuantity` value and unit separately, so a unit conversion is possible
  rather than a string comparison

## Value sets are seed data, never inline lists

Measures reference value sets by name; the codes live in the `value_set` table.
This mirrors how VSAC works in production, and the reason is not tidiness.

Value sets change. A new LOINC code for the same assay, an ICD-10 annual
revision, a corrected NCQA publication — when that happens you want to update
one seed row and re-run, not grep every measure for a literal. A measure with
inline codes keeps using last year's definition until someone notices, and
"someone notices" is usually the auditor.

`test_measures_reference_value_sets_not_inline_code_lists` fails if a code
literal appears in `measures.py`.

The OIDs in `warehouse.VALUE_SETS` are **illustrative placeholders, not real
VSAC OIDs**, and the code members are hand-built subsets. A production build
loads the real VSAC releases under licence and pins the version.

## Not dbt

dbt is not installed here. The structure mirrors a dbt project — staging →
marts → measures, value sets as seeds, rates pinned as regression tests — but
the models are SQL strings and Python functions executed in dependency order
against SQLite.

Missing as a result: the `ref()` graph and its lineage, incremental
materialisation, snapshots, dbt tests as declarations, the docs site, and any
notion of a model contract.
