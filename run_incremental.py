"""Incremental load, late-arriving data, and a measure restatement.

THE SCENARIO, which is the ordinary life of a HEDIS pipeline:

  1. The measurement year closes. The pipeline runs 30 days later on the data
     it has, the rates are marked FINAL, and they go to the health plan.
  2. Claims keep arriving. Most are for services in the closed year.
  3. The pipeline runs again in June on the full data.
  4. The rates are different. THAT IS NOT A BUG, and the pipeline has to be
     able to say so, quantify it, and name the cause.

A pipeline that cannot answer "did the number we published change, and by how
much" is not auditable, and HEDIS submissions are audited.

Run:  python run_incremental.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import incremental as INC
import measures as ME
import warehouse as WH

NDJSON = "data/bundles.ndjson"
DB = "out/incremental.db"
OUT = "out"

MY_START, MY_END = "2024-01-01", "2024-12-31"
EARLY_CLOSE = "2025-01-30"      # 30 days of runout: the submission-deadline run
LATE_RUN = "2025-06-30"         # six months of runout: the truth


def _load_window(ndjson, db_path, cutoff):
    """Build a warehouse from resources whose meta.lastUpdated is <= cutoff.

    This is what a `$export?_since=` window actually gives you: not the world
    as it was, but the world as the SERVER had recorded it by a moment. Every
    incremental bug in this domain comes from confusing the two.
    """
    if os.path.exists(db_path):
        os.remove(db_path)
    tmp = db_path + ".ndjson"
    kept = dropped = 0
    with open(ndjson, encoding="utf-8") as src, \
            open(tmp, "w", encoding="utf-8") as dst:
        for line in src:
            bundle = json.loads(line)
            entries = []
            for e in bundle.get("entry", []):
                lu = INC.last_updated(e["resource"])
                if lu is None or lu[:10] <= cutoff:
                    entries.append(e)
                    kept += 1
                else:
                    dropped += 1
            bundle["entry"] = entries
            dst.write(json.dumps(bundle) + "\n")
    loaded = WH.load(tmp, db_path)
    con = loaded[0] if isinstance(loaded, tuple) else loaded
    os.remove(tmp)
    return con, kept, dropped


def main():
    os.makedirs(OUT, exist_ok=True)
    print("=" * 78)
    print("INCREMENTAL LOAD AND RESTATEMENT")
    print("=" * 78)

    # ---- run 1: the submission-deadline run --------------------------------
    con1, kept1, dropped1 = _load_window(NDJSON, DB, EARLY_CLOSE)
    INC.init(con1)
    print(f"\nRUN 1 -- {EARLY_CLOSE} (30 days of runout, the submission run)")
    print(f"  resources visible {kept1:,}   not yet received {dropped1:,}")
    run1 = {}
    for name, fn in ME.MEASURES.items():
        r = fn(con1)
        rate = INC.record_run(con1, "run-1", name, "MY2024", len(r.denominator_ids),
                              len(r.numerator_ids), is_final=True, now=EARLY_CLOSE)
        run1[name] = r
        print(f"    {name:<10} {len(r.numerator_ids):>5}/{len(r.denominator_ids):<5} "
              f"= {rate:.4f}   FINAL, reported")

    # carry the finals into the second database
    finals = con1.execute("SELECT * FROM measure_run").fetchall()
    con1.close()

    # ---- run 2: six months of runout ---------------------------------------
    con2, kept2, dropped2 = _load_window(NDJSON, DB + ".2", LATE_RUN)
    INC.init(con2)
    for row in finals:
        con2.execute("INSERT OR REPLACE INTO measure_run VALUES (?,?,?,?,?,?,?,?)",
                     row)
    con2.commit()

    print(f"\nRUN 2 -- {LATE_RUN} (six months of runout)")
    print(f"  resources visible {kept2:,}   still outstanding {dropped2:,}")
    for name, fn in ME.MEASURES.items():
        r = fn(con2)
        rate = INC.record_run(con2, "run-2", name, "MY2024", len(r.denominator_ids),
                              len(r.numerator_ids), is_final=False, now=LATE_RUN)
        print(f"    {name:<10} {len(r.numerator_ids):>5}/{len(r.denominator_ids):<5} "
              f"= {rate:.4f}")

    # ---- what changed, and why ---------------------------------------------
    rs = INC.restatements(con2, "run-2", tolerance=0.0005)
    print("\n" + "-" * 78)
    print(f"RESTATEMENTS ({len(rs)})")
    print("-" * 78)
    if not rs:
        print("  none -- every published rate still computes the same")
    for r in rs:
        print(f"  {r['measure']} {r['period']}: "
              f"{r['reported_rate']:.4f} -> {r['current_rate']:.4f}  "
              f"({r['delta_pp']:+.2f} pp)")
        print(f"    denominator {r['reported_denominator']:,} -> "
              f"{r['current_denominator']:,}   "
              f"numerator {r['reported_numerator']:,} -> "
              f"{r['current_numerator']:,}")
        print(f"    {r['driver']}")

    print("\n  These are not errors. The same code on the same definitions")
    print("  gives a different answer because more of the year had been")
    print("  RECEIVED. A pipeline that silently overwrites the published")
    print("  number cannot answer the only question an auditor asks, which")
    print("  is what changed between the submission and today.")

    # ---- late arrivals ------------------------------------------------------
    closed = {"MY2024": (MY_START, MY_END, EARLY_CLOSE)}
    # First pass over the FULL export, remembering every version, so the
    # migration test below has a baseline to compare against.
    INC.scan(NDJSON, con2, closed_periods=(), remember=True)
    con2.commit()
    scan = INC.scan(NDJSON, con2, since=EARLY_CLOSE + "T00:00:00Z",
                    closed_periods=tuple(closed.items()))
    print("\n" + "-" * 78)
    print("A `_since` EXPORT TAKEN AFTER THE SUBMISSION RUN")
    print("-" * 78)
    c = scan["counts"]
    print(f"  would not be returned (lastUpdated <= watermark) {c['skipped-old']:>8,}")
    print(f"  new resources                                    {c['new']:>8,}")
    print(f"  changed resources                                {c['changed']:>8,}")
    print(f"  returned but content-identical                   "
          f"{c['unchanged-touch']:>8,}")
    print(f"\n  LATE ARRIVALS into the closed year: {len(scan['late_arrivals']):,}")
    if scan["late_arrivals"]:
        lags = sorted(l["lag_days"] for l in scan["late_arrivals"])
        print(f"    receipt lag, days: median {lags[len(lags)//2]}   "
              f"p90 {lags[int(len(lags)*0.9)]}   max {lags[-1]}")
        by_type = {}
        for l in scan["late_arrivals"]:
            by_type[l["resource_type"]] = by_type.get(l["resource_type"], 0) + 1
        for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
            print(f"    {t:<14}{n:>6,}")
    INC.commit_scan(con2, scan)

    print("\n  The watermark is meta.lastUpdated, NOT the clinical date. Using")
    print("  a clinical date as a watermark means a service performed in")
    print("  January and received in November is never loaded at all -- the")
    print("  watermark passed January ten months earlier. No error, no gap in")
    print("  the row count, just a measure rate that is quietly too low.")

    print("\n  'Returned but content-identical' is the case that matters after")
    print("  a server migration: meta.lastUpdated moves on ANY write, so a")
    print("  re-index makes a `_since` export return the entire dataset. A")
    print("  pipeline that treats 'returned' as 'changed' would restate every")
    print("  measure it has ever published. Content hashing is what prevents")
    print("  that, and it is why resource_version stores a hash of the body")
    print("  with meta excluded.")

    # ---- the migration test -------------------------------------------------
    # Every counter above reports 0 for 'returned but content-identical',
    # because nothing in this data has been touched without changing. A defence
    # whose counter has never moved is not a defence that has been shown to
    # work. So: simulate a server re-index that bumps meta.lastUpdated on every
    # resource and changes nothing else, which is exactly what a migration does.
    migrated = os.path.join(OUT, "_migrated.ndjson")
    n_touched = 0
    with open(NDJSON, encoding="utf-8") as src,             open(migrated, "w", encoding="utf-8") as dst:
        for line in src:
            bundle = json.loads(line)
            for e in bundle.get("entry", []):
                e["resource"].setdefault("meta", {})
                e["resource"]["meta"]["lastUpdated"] = "2025-07-01T00:00:00Z"
                n_touched += 1
            dst.write(json.dumps(bundle) + "\n")

    mig = INC.scan(migrated, con2, since="2025-06-30T00:00:00Z",
                   closed_periods=tuple(closed.items()))
    os.remove(migrated)
    mc = mig["counts"]
    print("")
    print("-" * 78)
    print("SIMULATED SERVER MIGRATION -- lastUpdated bumped, nothing else changed")
    print("-" * 78)
    print(f"  resources the `_since` export returns  {n_touched:>8,}")
    print(f"  classified as new                      {mc['new']:>8,}")
    print(f"  classified as changed                  {mc['changed']:>8,}")
    print(f"  recognised as content-identical        "
          f"{mc['unchanged-touch']:>8,}")
    print(f"  late arrivals raised                   "
          f"{len(mig['late_arrivals']):>8,}")
    print("")
    print("  A pipeline keying on lastUpdated alone would treat all")
    print(f"  {n_touched:,} of these as changes, reload the warehouse and")
    print("  restate every measure it has ever published -- from a re-index")
    print("  that changed no clinical fact at all. Content hashing is the")
    print("  whole defence, and this is it firing.")

    payload = {
        "run_1": {"as_of": EARLY_CLOSE, "visible": kept1, "outstanding": dropped1},
        "run_2": {"as_of": LATE_RUN, "visible": kept2, "outstanding": dropped2},
        "restatements": rs,
        "since_export": scan["counts"],
        "late_arrivals": len(scan["late_arrivals"]),
    }
    with open(f"{OUT}/incremental.json", "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    con2.close()
    print(f"\nwrote {OUT}/incremental.json")
    return payload


if __name__ == "__main__":
    main()
