"""Slowly-changing dimensions, and a merge that applies a delta.

TWO NAMED GAPS
--------------
"no snapshot/SCD2 history on dimensions, so a patient's race recorded
differently over time overwrites rather than versions, and a stratified rate
cannot be recomputed as it stood."

"`run_incremental.py` rebuilds a window rather than applying a delta. It filters
the export by `lastUpdated` and loads that, which proves the watermark semantics
but is not merge/upsert against a live warehouse."

WHY OVERWRITING A DIMENSION BREAKS A REPORTED MEASURE
------------------------------------------------------
The stratified HEDIS rates are computed by race and ethnicity. Those come from
US Core extensions on the Patient resource, and a Patient resource is MUTABLE:
a registration clerk corrects a field, a data-quality project backfills
self-reported race over an inferred value, a merge consolidates two records.

If the warehouse overwrites, the disparity gap published in February cannot be
reproduced in June -- not because the measure changed, but because the
DENOMINATOR'S ATTRIBUTES changed underneath it. The rate is recomputable; the
stratification is not. And a disparity finding that cannot be reproduced is a
disparity finding that cannot be defended.

SCD2 fixes it by never updating in place. Each version gets `valid_from`,
`valid_to` and `is_current`, so "what did we believe this patient's race was on
2025-01-30" is answerable, and the February report can be reconstructed exactly.

THE PART THAT IS EASY TO GET WRONG
-----------------------------------
A no-op update must NOT create a version. FHIR's `meta.lastUpdated` moves on any
write, so a server re-index would otherwise produce a new row per patient per
migration, and the history becomes noise that hides the three real changes in
it. `upsert()` compares the tracked attributes and returns "unchanged" -- the
same content-hash discipline `incremental.py` uses, applied to dimensions.

The second trap is the boundary. `valid_to` is EXCLUSIVE and the previous
version's `valid_to` equals the new version's `valid_from`, so a point-in-time
query with `valid_from <= t < valid_to` returns exactly one row. Using an
inclusive `valid_to` set to "the day before" breaks the moment two changes land
on the same day, which is precisely when a data-quality project is running.

WHAT THIS IS NOT
----------------
No bitemporality -- there is one time axis (when we believed it), not two (when
it was true AND when we believed it). Real clinical data wants both: a race
correction applies retroactively to when the patient was registered, not from
the day the clerk fixed it, and separating those needs a second pair of columns.
No late-arriving-dimension handling, no surrogate keys, no schema evolution.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS patient_dim (
    patient_id       TEXT NOT NULL,
    version          INTEGER NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_to         TEXT,              -- NULL means current; EXCLUSIVE bound
    is_current       INTEGER NOT NULL,
    birth_date       TEXT,
    gender           TEXT,
    race_code        TEXT,
    race_display     TEXT,
    ethnicity_code   TEXT,
    ethnicity_display TEXT,
    change_reason    TEXT,
    PRIMARY KEY (patient_id, version)
);

CREATE INDEX IF NOT EXISTS idx_patient_dim_current
    ON patient_dim (patient_id, is_current);
"""

TRACKED = ("birth_date", "gender", "race_code", "race_display",
           "ethnicity_code", "ethnicity_display")


def init(con):
    con.executescript(SCHEMA)
    return con


def current(con, patient_id):
    row = con.execute(
        "SELECT * FROM patient_dim WHERE patient_id=? AND is_current=1",
        (patient_id,)).fetchone()
    return _row(con, row)


def as_of(con, patient_id, when):
    """What did we believe on `when`?

    `valid_from <= when < valid_to` -- the exclusive upper bound is what makes
    this return exactly one row even when two versions land on the same day.
    """
    row = con.execute(
        "SELECT * FROM patient_dim WHERE patient_id=? AND valid_from <= ? "
        "AND (valid_to IS NULL OR valid_to > ?) ORDER BY version DESC LIMIT 1",
        (patient_id, when, when)).fetchone()
    return _row(con, row)


def _row(con, row):
    if row is None:
        return None
    cols = [d[0] for d in con.execute(
        "SELECT * FROM patient_dim LIMIT 0").description]
    return dict(zip(cols, row))


def history(con, patient_id):
    rows = con.execute(
        "SELECT * FROM patient_dim WHERE patient_id=? ORDER BY version",
        (patient_id,)).fetchall()
    cols = [d[0] for d in con.execute(
        "SELECT * FROM patient_dim LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


def upsert(con, patient_id, attrs, effective_at, reason=""):
    """Version the row only if a TRACKED attribute actually changed.

    Returns "inserted", "versioned" or "unchanged". The third is the important
    one: FHIR's meta.lastUpdated moves on any write, so without this a server
    re-index produces a new version per patient per migration and the history
    becomes noise that hides the real changes inside it.
    """
    cur = current(con, patient_id)
    new = {k: attrs.get(k) for k in TRACKED}

    if cur is None:
        con.execute(
            "INSERT INTO patient_dim (patient_id, version, valid_from, "
            "valid_to, is_current, birth_date, gender, race_code, "
            "race_display, ethnicity_code, ethnicity_display, change_reason) "
            "VALUES (?,?,?,NULL,1,?,?,?,?,?,?,?)",
            (patient_id, 1, effective_at, new["birth_date"], new["gender"],
             new["race_code"], new["race_display"], new["ethnicity_code"],
             new["ethnicity_display"], reason or "initial load"))
        return "inserted"

    if all(cur[k] == new[k] for k in TRACKED):
        return "unchanged"

    changed = [k for k in TRACKED if cur[k] != new[k]]
    con.execute(
        "UPDATE patient_dim SET valid_to=?, is_current=0 "
        "WHERE patient_id=? AND version=?",
        (effective_at, patient_id, cur["version"]))
    con.execute(
        "INSERT INTO patient_dim (patient_id, version, valid_from, valid_to, "
        "is_current, birth_date, gender, race_code, race_display, "
        "ethnicity_code, ethnicity_display, change_reason) "
        "VALUES (?,?,?,NULL,1,?,?,?,?,?,?,?)",
        (patient_id, cur["version"] + 1, effective_at, new["birth_date"],
         new["gender"], new["race_code"], new["race_display"],
         new["ethnicity_code"], new["ethnicity_display"],
         reason or f"changed: {', '.join(changed)}"))
    return "versioned"


def stratum_as_of(con, patient_ids, when, field="race_code"):
    """The stratification as it stood on `when`.

    THE FUNCTION THE WHOLE MODULE EXISTS FOR. It is what makes a published
    disparity finding reproducible: recomputing the February report in June
    needs February's denominators AND February's attributes, and only the
    second one is lost by an overwriting warehouse.
    """
    out = {}
    for pid in patient_ids:
        row = as_of(con, pid, when)
        out[pid] = row[field] if row else None
    return out


def drift(con, patient_ids, t0, t1, field="race_code"):
    """How many patients' stratum changed between two dates?

    Reported as a count AND as the specific movements, because "3 patients
    moved between race categories" and "3 patients moved from unknown to a
    recorded value" are different events with different consequences for a
    disparity report -- the second shrinks an 'unknown' bucket that was
    suppressing the gap.
    """
    moves = {}
    n = 0
    for pid in patient_ids:
        a = as_of(con, pid, t0)
        b = as_of(con, pid, t1)
        va = a[field] if a else None
        vb = b[field] if b else None
        if va != vb:
            n += 1
            moves[(va, vb)] = moves.get((va, vb), 0) + 1
    return {"n_changed": n, "n_patients": len(patient_ids),
            "movements": [{"from": k[0], "to": k[1], "n": v}
                          for k, v in sorted(moves.items(),
                                             key=lambda kv: -kv[1])]}


# ---------------------------------------------------------------------------
# merge/upsert of facts
# ---------------------------------------------------------------------------

def merge_facts(con, table, rows, key_cols, all_cols):
    """Apply a DELTA to a fact table: insert new rows, update changed ones.

    The second named gap. `run_incremental.py` rebuilt a filtered window, which
    proves the watermark semantics and is not what a warehouse does. This is
    the merge: existing rows are updated in place, new ones inserted, and
    UNCHANGED ROWS ARE NOT TOUCHED.

    That last part is not an optimisation. A merge that rewrites every row on
    every run destroys the one signal an operator has -- "how much actually
    changed last night" -- and turns a 12-row delta into a full-table rewrite
    that looks identical to a corruption.
    """
    placeholders = ",".join("?" * len(all_cols))
    where = " AND ".join(f"{k}=?" for k in key_cols)
    stats = {"inserted": 0, "updated": 0, "unchanged": 0}

    for r in rows:
        key = tuple(r[k] for k in key_cols)
        existing = con.execute(
            f"SELECT {','.join(all_cols)} FROM {table} WHERE {where}",
            key).fetchone()
        values = tuple(r.get(c) for c in all_cols)
        if existing is None:
            con.execute(f"INSERT INTO {table} ({','.join(all_cols)}) "
                        f"VALUES ({placeholders})", values)
            stats["inserted"] += 1
        elif tuple(existing) == values:
            stats["unchanged"] += 1
        else:
            sets = ",".join(f"{c}=?" for c in all_cols)
            con.execute(f"UPDATE {table} SET {sets} WHERE {where}",
                        values + key)
            stats["updated"] += 1
    return stats
