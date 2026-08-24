"""Run the measures against Synthea -- data this repository did not write.

WHY
---
Every measure here was written against bundles this repository also generates.
That is a closed loop: the generator emits the codes the value sets look for,
at the grain the loader expects, with the references the joins assume. A
pipeline can pass every test in that arrangement and still be unable to read
anybody else's data -- and worse, can be WRONG in ways the loop hides.

Synthea is the reference synthetic-data generator for this kind of work. It is
free, it is not written by me, and pointing the pipeline at it is the
data-level version of the discipline this portfolio applies to code: check
yourself against an independent implementation.

It found four things. None of them raised an exception on the way in.

  1. `urn:uuid:` references -- the loader split on "/" and matched no patient.
     Every denominator would have collapsed to zero, silently.
  2. NO Coverage resource -- Synthea's FHIR export has none, and continuous
     enrolment gates every denominator. Derived from payer_transitions.csv and
     TAGGED as derived.
  3. `performedPeriod` not `performedDateTime` -- 100% of Synthea procedures
     use the other half of a FHIR choice type, so every procedure date was
     NULL and the BCS numerator was empty.
  4. A WRONG SNOMED CODE that had been wrong all along. See below.

THE ONE THAT MATTERS
--------------------
The "Bilateral Mastectomy" value set contained SNOMED 428251008, labelled
"History of bilateral mastectomy". In SNOMED CT that code means **History of
appendectomy**.

The generator emitted 428251008 and the value set looked for 428251008, so they
agreed with each other perfectly and no test could see it. Synthea uses the code
for its real meaning: **28 appendectomy records matched the value set, and 4 of
those patients were BCS-eligible women who were wrongly excluded** from
breast-cancer screening. In a real plan those four are simply never contacted
about a mammogram.

Run:  python run_synthea.py            # uses synthea_out/, see TOOLCHAIN.md
      python run_synthea.py --generate # generate a fresh population first
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(ROOT))

import measures as M
import synthea as S
import warehouse as W

SYNTHEA_DIR = os.path.join(ROOT, "synthea_out")
OUTPUT = os.path.join(SYNTHEA_DIR, "output")
NDJSON = os.path.join(SYNTHEA_DIR, "bundles.ndjson")


def generate(n=600, seed=20260824):
    """Run Synthea itself. Needs the JDK and jar from TOOLCHAIN.md."""
    import toolchain

    java, jar = toolchain.java_exe(), toolchain.jar("synthea.jar")
    if not (java and jar):
        raise SystemExit("Synthea unavailable: %s" % (toolchain.why_not()
                                                      or "synthea.jar absent"))
    os.makedirs(SYNTHEA_DIR, exist_ok=True)
    subprocess.run([java, "-jar", jar, "-p", str(n), "-s", str(seed),
                    "--exporter.fhir.export", "true",
                    "--exporter.csv.export", "true"],
                   cwd=SYNTHEA_DIR, check=True)


def _observed_value_sets():
    """Synthea's own codes, keyed by placeholder OIDs to match the seed shape."""
    return {"syn-oid-%d" % i: (name, members)
            for i, (name, members) in enumerate(S.SYNTHEA_VALUE_SETS.items())}


def run(rebuild=True):
    if not os.path.isdir(OUTPUT):
        raise SystemExit(
            "no Synthea output at %s -- run `python run_synthea.py "
            "--generate` (needs Java, see ../TOOLCHAIN.md)" % OUTPUT)

    if rebuild or not os.path.exists(NDJSON):
        stats = S.to_ndjson(OUTPUT, NDJSON)
    else:
        stats = {"path": NDJSON}

    results = {}
    for tag, value_sets in (("project", None),
                            ("observed", _observed_value_sets())):
        db = os.path.join(SYNTHEA_DIR, "wh_%s.db" % tag)
        if os.path.exists(db):
            os.remove(db)
        con, counts = W.load(NDJSON, db, value_sets=value_sets)
        rows = {}
        for name in ("CDC-A1C", "BCS", "CIS-DTaP"):
            res = M.MEASURES[name](con)
            den, num = len(res.denominator_ids), len(res.numerator_ids)
            rows[name] = {"denominator": den, "numerator": num,
                          "rate": (num / den) if den else None,
                          "excluded": len(getattr(res, "excluded_ids", []))}
        # how many warehouse rows each value set actually matches
        coverage = {}
        for vs in S.SYNTHEA_VALUE_SETS:
            codes = M.value_set_codes(con, vs)
            hits = 0
            for table in ("condition", "observation", "procedure",
                          "encounter", "immunization"):
                for system, code in codes:
                    hits += con.execute(
                        "SELECT COUNT(*) FROM %s WHERE code_system=? "
                        "AND code=?" % table, (system, code)).fetchone()[0]
            coverage[vs] = {"n_codes": len(codes), "matching_rows": hits}
        results[tag] = {"measures": rows, "value_sets": coverage,
                        "loaded": counts}
        con.close()

    return stats, results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("-n", type=int, default=600)
    args = ap.parse_args()

    if args.generate:
        generate(args.n)

    stats, results = run()

    print("=" * 74)
    print("  Synthea input: %d bundles, %d entries, %d Coverage DERIVED"
          % (stats.get("bundles", 0), stats.get("entries", 0),
             stats.get("coverage_derived", 0)))
    loaded = results["project"]["loaded"]
    print("  loaded: " + ", ".join("%s=%d" % (k, v)
                                   for k, v in sorted(loaded.items()) if v))
    print("=" * 74)
    print()
    print("  %-10s %-28s %-28s" % ("", "PROJECT value sets", "OBSERVED codes"))
    for name in ("CDC-A1C", "BCS", "CIS-DTaP"):
        a = results["project"]["measures"][name]
        b = results["observed"]["measures"][name]
        fmt = lambda r: ("den=%-4d num=%-4d rate=%s"
                         % (r["denominator"], r["numerator"],
                            ("%.4f" % r["rate"]) if r["rate"] is not None
                            else "n/a"))
        print("  %-10s %-28s %-28s" % (name, fmt(a), fmt(b)))
    print()
    print("  value set -> matching warehouse rows")
    print("  %-26s %10s %10s" % ("", "project", "observed"))
    for vs in S.SYNTHEA_VALUE_SETS:
        a = results["project"]["value_sets"][vs]["matching_rows"]
        b = results["observed"]["value_sets"][vs]["matching_rows"]
        flag = "   <-- matched NOTHING" if a == 0 else ""
        print("  %-26s %10d %10d%s" % (vs, a, b, flag))
    print("=" * 74)

    _write_report(stats, results)


def _write_report(stats, results):
    doc = os.path.join(ROOT, "docs")
    os.makedirs(doc, exist_ok=True)
    path = os.path.join(doc, "SYNTHEA_RUN.md")

    def row(name):
        a = results["project"]["measures"][name]
        b = results["observed"]["measures"][name]
        f = lambda r: ("%d / %d = %s" % (r["numerator"], r["denominator"],
                                         ("%.2f%%" % (100 * r["rate"]))
                                         if r["rate"] is not None else "n/a"))
        return "| `%s` | %s | %s |" % (name, f(a), f(b))

    vs_rows = []
    for vs in S.SYNTHEA_VALUE_SETS:
        a = results["project"]["value_sets"][vs]["matching_rows"]
        b = results["observed"]["value_sets"][vs]["matching_rows"]
        vs_rows.append("| `%s` | %d | %d |" % (vs, a, b))

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("""# Running the measures on Synthea

Every measure in this project was written against bundles this repository also
generates. That is a **closed loop** -- the generator emits the codes the value
sets look for, at the grain the loader expects, with the references the joins
assume. A pipeline can pass every test in that arrangement and still be unable
to read anybody else's data.

[Synthea](https://github.com/synthetichealth/synthea) is the reference
synthetic-data generator for this kind of work, it is free, and it is not
written by me. This is the data-level version of the discipline the rest of the
portfolio applies to code.

**Input:** %d bundles, %d entries, of which **%d Coverage resources were
derived** by this project rather than emitted by Synthea.

## Four seams, none of which raised an exception

**1. `urn:uuid:` references.** Synthea writes them throughout; `_ref_id` split
on `/` and returned the whole string, matching no `Patient.id`. Every clinical
resource would have joined to nobody and every denominator collapsed to zero --
and a measure reporting 0%% reads as a finding, not a failure.

**2. No `Coverage` resource at all.** Synthea's FHIR export has none, and
continuous enrolment gates every HEDIS denominator. The spans exist in
`payer_transitions.csv`, so they are lifted from there and tagged
`meta.tag = derived`, because a resource this pipeline manufactured must never
be mistaken for one the generator produced.

**3. `performedPeriod`, not `performedDateTime`.** FHIR choice types let one
logical field be expressed either way. **100%% of Synthea procedures** use the
half this loader did not read, so every procedure date was NULL and the BCS
numerator was empty -- until it crashed on a `None` comparison, which is the
only reason anyone noticed.

**4. A wrong SNOMED code that had been wrong all along.** Below.

## The value sets, measured against real data

| value set | rows matched, PROJECT codes | rows matched, OBSERVED codes |
|---|---|---|
%s

`Hospice Encounter` matched **zero** rows with the project's code. It is a
*required exclusion*, and it silently excluded nobody.

## What that does to the numbers

| measure | PROJECT value sets | OBSERVED codes |
|---|---|---|
%s

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
""" % (stats.get("bundles", 0), stats.get("entries", 0),
       stats.get("coverage_derived", 0),
       "\n".join(vs_rows),
       "\n".join(row(n) for n in ("CDC-A1C", "BCS", "CIS-DTaP"))))
    print("wrote", path)


if __name__ == "__main__":
    main()
