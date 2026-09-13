"""Validate the generated FHIR corpus against the R4B schema models.

WHY THIS EXISTS
---------------
`src/fhir_gen.py` writes FHIR R4 bundles by hand. Every measure in this project
reads them, and every test asserts things about the numbers those measures
produce -- so the corpus was checked extensively for whether it says the right
things, and never once for whether it is valid FHIR.

Those are different questions, and the second one had a bad answer.

WHAT IT FOUND
-------------
`Coverage.payor` is REQUIRED in R4 (cardinality 1..*) and the generator omitted
it. Every Coverage resource in the corpus -- all 82 of them -- was invalid
FHIR. The measure logic never noticed, because continuous-enrolment only reads
`period`. A real FHIR server would have rejected the lot on ingest.

That is the failure mode worth naming: **the corpus was tested against the
consumer it happened to have, not against the standard it claimed to follow.**

MIND THE VERSION
----------------
`fhir.resources` defaults to R5. This project targets R4, so the R4B models are
imported explicitly. Validating R4 resources against R5 models produces
failures that are version differences rather than bugs.

WHAT THIS DOES NOT DO
---------------------
Schema validity is a floor. No US Core profiles, no `meta.profile`, no
terminology-server validation of the code systems, no HEDIS value-set
certification. A bundle can be structurally perfect and clinically nonsense.

Run:  python validate_fhir.py
"""

from __future__ import annotations

import collections
import importlib
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    _MODULES = {
        "Patient": "patient", "Coverage": "coverage", "Condition": "condition",
        "Observation": "observation", "Procedure": "procedure",
        "Encounter": "encounter", "Immunization": "immunization",
        "Bundle": "bundle",
    }
    MODELS = {
        rt: getattr(importlib.import_module("fhir.resources.R4B." + mod), rt)
        for rt, mod in _MODULES.items()
    }
except ImportError as exc:                              # pragma: no cover
    print("fhir.resources not installed (%s)." % exc)
    print("This is an OPTIONAL audit; src/ does not depend on it.")
    raise SystemExit(0)

NDJSON = os.path.join(ROOT, "data", "bundles.ndjson")


def validate(path=NDJSON):
    """Validate every bundle and every resource inside it."""
    if not os.path.exists(path):
        raise SystemExit("no corpus at %s -- run `python -m src.fhir_gen` or "
                         "the generate step in the README first" % path)

    valid = collections.Counter()
    invalid = collections.Counter()
    examples = {}
    bundles_ok = bundles_bad = 0

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            bundle = json.loads(line)
            try:
                MODELS["Bundle"].model_validate(bundle)
                bundles_ok += 1
            except Exception as exc:
                bundles_bad += 1
                examples.setdefault("Bundle", _first_error(exc))

            for entry in bundle.get("entry", []):
                res = entry.get("resource", entry)
                model = MODELS.get(res.get("resourceType"))
                if model is None:
                    continue
                try:
                    model.model_validate(res)
                    valid[res["resourceType"]] += 1
                except Exception as exc:
                    invalid[res["resourceType"]] += 1
                    examples.setdefault(res["resourceType"], _first_error(exc))

    return {
        "bundles_valid": bundles_ok,
        "bundles_invalid": bundles_bad,
        "valid": dict(valid),
        "invalid": dict(invalid),
        "examples": examples,
        "total_valid": sum(valid.values()),
        "total_invalid": sum(invalid.values()),
    }


def _first_error(exc):
    lines = str(exc).splitlines()
    return lines[1].strip() if len(lines) > 1 else str(exc)[:120]


def main():
    r = validate()

    print("=" * 70)
    print("  bundles      valid %d   invalid %d"
          % (r["bundles_valid"], r["bundles_invalid"]))
    print("  resources    valid %d   invalid %d"
          % (r["total_valid"], r["total_invalid"]))
    print()
    for rt in sorted(set(r["valid"]) | set(r["invalid"])):
        print("     %-14s valid %4d   invalid %4d"
              % (rt, r["valid"].get(rt, 0), r["invalid"].get(rt, 0)))
    for rt, err in r["examples"].items():
        print("     %-14s first error: %s" % (rt, err))
    print("=" * 70)

    doc = os.path.join(ROOT, "docs")
    os.makedirs(doc, exist_ok=True)
    path = os.path.join(doc, "FHIR_VALIDATION.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("""# FHIR R4B validation of the generated corpus

`src/fhir_gen.py` writes FHIR R4 bundles by hand. Every measure reads them and
every test asserts things about the resulting rates -- so the corpus was checked
exhaustively for whether it says the right things, and never once for whether
it is **valid FHIR**.

Those are different questions, and the second had a bad answer.

## Result

| | valid | invalid |
|---|---|---|
| bundles | %d | %d |
| resources | **%d** | **%d** |

%s

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
""" % (r["bundles_valid"], r["bundles_invalid"],
       r["total_valid"], r["total_invalid"],
       "\n".join("- `%s`: %d valid, %d invalid"
                 % (rt, r["valid"].get(rt, 0), r["invalid"].get(rt, 0))
                 for rt in sorted(set(r["valid"]) | set(r["invalid"])))))
    print("wrote", path)


if __name__ == "__main__":
    main()
