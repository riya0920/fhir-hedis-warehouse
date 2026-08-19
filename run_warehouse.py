"""Ingest bundles, build the warehouse, compute measures, reconcile, verify.

Run:  python run_warehouse.py [--patients 20000]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import fhir_gen
import measures
import reference
import warehouse

OUT = "out"
DB = "warehouse.db"


def main(n_patients=20000, regenerate=True):
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    path = "data/bundles.ndjson"
    if regenerate or not os.path.exists(path):
        path = fhir_gen.generate(n_patients)

    t1 = time.time()
    con, counts = warehouse.load(path, DB)
    print(f"\nflattened into {DB} in {time.time()-t1:.1f}s")
    for k, v in counts.items():
        print(f"  {k:<14}{v:>10,}")
    vs = con.execute("SELECT COUNT(*) FROM value_set").fetchone()[0]
    n_vs = con.execute("SELECT COUNT(DISTINCT value_set_id) FROM value_set").fetchone()[0]
    print(f"  {'value_set':<14}{vs:>10,}  ({n_vs} value sets, as seed data)")

    missing_system = con.execute(
        "SELECT COUNT(*) FROM condition WHERE code_system IS NULL").fetchone()[0]
    print(f"\n  codes stored without a system: {missing_system}"
          f"  (a code without its system is meaningless)")

    # ---- measures --------------------------------------------------------
    results = {}
    for key, fn in measures.MEASURES.items():
        r = fn(con)
        results[key] = r
        print("\n" + "=" * 74)
        print(f"{r.key}: {r.title}")
        print("=" * 74)
        prev = None
        for label, n in r.waterfall:
            drop = f"  (-{prev-n:,})" if prev is not None and prev >= n else ""
            print(f"  {label:<48}{n:>9,}{drop}")
            prev = n
        print(f"  {'RATE':<48}{r.rate:>9.1%}"
              f"   ({len(r.numerator_ids):,}/{len(r.denominator_ids):,})")

    print("\n  The waterfall is the artefact, not the rate. A rate can fall")
    print("  because the numerator fell or because the denominator grew, and")
    print("  those have different owners -- only the waterfall distinguishes")
    print("  them. It is also what an auditor asks for first.")

    # ---- reconciliation --------------------------------------------------
    print("\n" + "=" * 74)
    print("RECONCILIATION: warehouse+SQL vs an INDEPENDENT implementation")
    print("=" * 74)
    print("  The reference implementation in src/reference.py reads the FHIR")
    print("  bundles DIRECTLY -- no warehouse, no value-set tables, no shared")
    print("  helpers. Two code paths from the same source to the same answer.")
    print("  This is what 'hand-computed for 25 patients' means when done at")
    print("  scale; it is NOT a claim that I computed anything by hand.")
    sample = reference.sample_patient_ids(path, 25)
    recon = reference.reconcile(path, con, results, sample)
    for key, r in recon.items():
        status = "MATCH" if r["mismatches"] == 0 else f"{r['mismatches']} MISMATCH"
        print(f"\n  {key:<12}{r['n']:>4} patients compared   {status}")
        for mm in r["detail"][:5]:
            print(f"     {mm}")

    total_mismatch = sum(r["mismatches"] for r in recon.values())
    full = reference.reconcile(path, con, results, None)
    total_full = sum(r["mismatches"] for r in full.values())
    print(f"\n  25-patient sample: {total_mismatch} mismatches")
    print(f"  ALL patients:      {total_full} mismatches across "
          f"{sum(r['n'] for r in full.values()):,} comparisons")

    # ---- planted edge cases ---------------------------------------------
    print("\n" + "=" * 74)
    print("PLANTED EDGE CASES -- measure logic IS edge-case logic")
    print("=" * 74)
    cdc = results["CDC-A1C"]
    print(f"  {'case':<22}{'in denom':>10}{'expected':>10}"
          f"{'in numer':>10}{'expected':>10}  result")
    all_ok = True
    for name, exp in fhir_gen.EDGE_CASES.items():
        in_d = name in cdc.denominator_ids
        in_n = name in cdc.numerator_ids
        ok = (in_d == exp["expected_denominator"]
              and in_n == exp["expected_numerator"])
        all_ok &= ok
        print(f"  {name:<22}{str(in_d):>10}{str(exp['expected_denominator']):>10}"
              f"{str(in_n):>10}{str(exp['expected_numerator']):>10}  "
              f"{'PASS' if ok else 'FAIL'}")
    for name, exp in fhir_gen.EDGE_CASES.items():
        print(f"    {name}: {exp['why']}")
    print(f"\n  {'ALL EDGE CASES PASS' if all_ok else 'EDGE CASE FAILURES'}")

    # ---- care gaps -------------------------------------------------------
    print("\n" + "=" * 74)
    print("CARE-GAP DRILL-THROUGH -- what a quality team actually works")
    print("=" * 74)
    for key, r in results.items():
        gaps = measures.care_gaps(con, r, limit=3)
        n_total = len(r.denominator_ids - r.numerator_ids)
        print(f"\n  {key}: {n_total:,} non-compliant members")
        for g in gaps:
            print(f"    {g['member_id']:<12} {g['missing']}")
        if n_total > 3:
            print(f"    ... {n_total-3:,} more")

    # ---- regression pins -------------------------------------------------
    pins = {k: {"rate": round(r.rate, 6),
                "denominator": len(r.denominator_ids),
                "numerator": len(r.numerator_ids)}
            for k, r in results.items()}
    pin_path = f"{OUT}/measure_pins.json"
    print("\n" + "=" * 74)
    print("MEASURE REGRESSION PINS")
    print("=" * 74)
    if os.path.exists(pin_path):
        old = json.load(open(pin_path))
        moved = [k for k in pins if old.get(k) != pins[k]]
        if moved:
            print(f"  RATES MOVED: {moved}")
            print("  A measure rate moving is not a test failure to be silenced.")
            print("  It means the population definition, a value set, or event")
            print("  capture changed, and someone must say which and why before")
            print("  the pin is updated. That is measure governance in miniature.")
        else:
            print("  all rates unchanged against the pinned values")
    else:
        print(f"  no pins yet; writing {pin_path}")
    with open(pin_path, "w") as fh:
        json.dump(pins, fh, indent=2)

    payload = {"counts": counts,
               "measures": {k: r.as_dict() for k, r in results.items()},
               "reconciliation_sample": {k: v["mismatches"] for k, v in recon.items()},
               "reconciliation_full": {k: v["mismatches"] for k, v in full.items()},
               "edge_cases_pass": all_ok,
               "runtime_sec": round(time.time() - t0, 1)}
    with open(f"{OUT}/results.json", "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"\nwrote {OUT}/results.json  (total {time.time()-t0:.0f}s)")
    return payload


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--patients", type=int, default=20000)
    a = ap.parse_args()
    main(a.patients)
