"""Incremental load, late-arriving data, and measure restatement.

WHY meta.lastUpdated AND NOT THE CLINICAL DATE
----------------------------------------------
FHIR already specifies the incremental mechanism: `$export?_since=<instant>`
returns resources whose `meta.lastUpdated` is at or after that instant. So the
watermark is `meta.lastUpdated`, not `onsetDateTime` or `effectiveDateTime`.

That distinction is the whole file. A HEDIS measure is computed from CLINICAL
dates -- was the A1c drawn during the measurement year -- while the pipeline is
fed by RECEIPT order. The two are unrelated, and treating the clinical date as
a watermark means a claim for a January service that arrives in November is
never loaded at all, because the watermark passed January ten months ago.

That failure is silent. No error, no gap in the row count, just a measure rate
that is quietly too low forever.

WHAT `_since` DOES NOT GIVE YOU, which is the part worth knowing:

  * `meta.lastUpdated` changes on ANY write, including ones with no clinical
    content -- a demographics correction, a re-index, a bulk migration. A
    `_since` export after a server migration returns the entire dataset, and
    a pipeline that assumes "returned means changed" will restate every
    measure it has ever published.
  * Non-conformant servers do not always bump it. FHIR requires the server to
    maintain it, and servers that do not leave you silently missing updates
    that a `_since` export will never return again.
  * DELETES are not in a `_since` export at all. A resource that was retracted
    is simply absent, and absence is indistinguishable from "not changed".
    FHIR added `$export?_typeFilter` and deleted-resource reporting later; a
    pipeline that only ever appends will keep counting a retracted A1c result.

Only the first is handled here (by comparing content hashes, so a no-op update
does not trigger a restatement). The other two are stated, not solved.

RESTATEMENT
-----------
The hard part of incremental HEDIS is not loading. It is that a late claim can
change a measure year that has already been REPORTED. The rate that went to the
health plan's board in February is not the rate the same code computes in June,
and the difference is not a bug.

So every measure run is recorded in `measure_run`, and `restatements()` compares
the current computation of a CLOSED period against what was reported for it.
A pipeline that cannot answer "did the number we published change, and by how
much" is not auditable, and HEDIS submissions are audited.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

INCREMENTAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS load_watermark (
    source        TEXT PRIMARY KEY,
    last_updated  TEXT NOT NULL,     -- the _since value for the next export
    loaded_at     TEXT NOT NULL,
    n_resources   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_version (
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    last_updated  TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS late_arrival (
    resource_type  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    clinical_date  TEXT,
    last_updated   TEXT NOT NULL,
    lag_days       INTEGER,
    closed_period  TEXT,
    detected_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS measure_run (
    run_id        TEXT NOT NULL,
    run_at        TEXT NOT NULL,
    measure       TEXT NOT NULL,
    period        TEXT NOT NULL,
    denominator   INTEGER NOT NULL,
    numerator     INTEGER NOT NULL,
    rate          REAL NOT NULL,
    is_final      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, measure, period)
);
"""

# The clinical date field per resource type. Chosen per type rather than
# guessed, because a Procedure has no onsetDateTime and an Observation has no
# performedDateTime, and a generic getattr-style lookup returns None for half
# the corpus while looking like it worked.
CLINICAL_DATE_FIELD = {
    "Condition": "onsetDateTime",
    "Observation": "effectiveDateTime",
    "Procedure": "performedDateTime",
    "Immunization": "occurrenceDateTime",
    "Encounter": None,          # period.start, handled specially
    "Patient": None,
    "Coverage": None,
}


def init(con):
    con.executescript(INCREMENTAL_SCHEMA)
    return con


def content_hash(resource):
    """Hash of the resource WITHOUT meta, so a no-op touch is detectable.

    This is what stops a server migration from restating every measure. If
    `meta.lastUpdated` moved but nothing else did, the resource is returned by
    a `_since` export and must be recognised as unchanged.
    """
    body = {k: v for k, v in resource.items() if k != "meta"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def clinical_date(resource):
    rt = resource.get("resourceType")
    field = CLINICAL_DATE_FIELD.get(rt, "__unknown__")
    if field == "__unknown__":
        return None
    if rt == "Encounter":
        return (resource.get("period") or {}).get("start")
    return resource.get(field) if field else None


def last_updated(resource):
    return (resource.get("meta") or {}).get("lastUpdated")


def get_watermark(con, source="bulk-export"):
    row = con.execute("SELECT last_updated FROM load_watermark WHERE source=?",
                      (source,)).fetchone()
    return row[0] if row else None


def classify(con, resource, closed_periods=()):
    """What kind of change is this, and is it late?

    Returns one of: 'new', 'changed', 'unchanged-touch', and a late-arrival
    record if the clinical date falls inside a period already reported.
    """
    rt, rid = resource.get("resourceType"), resource.get("id")
    h = content_hash(resource)
    lu = last_updated(resource)
    row = con.execute(
        "SELECT content_hash FROM resource_version "
        "WHERE resource_type=? AND resource_id=?", (rt, rid)).fetchone()

    if row is None:
        kind = "new"
    elif row[0] == h:
        # THE MIGRATION CASE. lastUpdated moved, content did not. Counting this
        # as a change would restate every measure after a server re-index.
        kind = "unchanged-touch"
    else:
        kind = "changed"

    cd = clinical_date(resource)
    late = None
    if cd and lu and kind != "unchanged-touch":
        for period, (p_start, p_end, closed_at) in dict(closed_periods).items():
            if p_start <= cd[:10] <= p_end and lu[:10] > closed_at:
                late = {
                    "resource_type": rt, "resource_id": rid,
                    "clinical_date": cd[:10], "last_updated": lu[:10],
                    "lag_days": (datetime.fromisoformat(lu[:10])
                                 - datetime.fromisoformat(cd[:10])).days,
                    "closed_period": period,
                }
                break
    return kind, h, lu, late


def record(con, resource, kind, h, lu, now):
    rt, rid = resource.get("resourceType"), resource.get("id")
    if kind == "new":
        con.execute("INSERT INTO resource_version VALUES (?,?,?,?,?)",
                    (rt, rid, lu, h, now))
    elif kind == "changed":
        con.execute("UPDATE resource_version SET last_updated=?, content_hash=? "
                    "WHERE resource_type=? AND resource_id=?", (lu, h, rt, rid))


def scan(ndjson_path, con, *, since=None, closed_periods=(), now=None,
         remember=False):
    """Classify an export without loading it, so the decision is inspectable.

    Separate from `load` on purpose. "What would this batch change" is a
    question an operator needs answered BEFORE a run that might restate a
    published measure, and a loader that answers it only by doing it is not
    much of an answer.
    """
    now = now or datetime.now().isoformat(timespec="seconds")
    counts = {"new": 0, "changed": 0, "unchanged-touch": 0, "skipped-old": 0}
    lates, max_lu = [], since

    with open(ndjson_path, encoding="utf-8") as fh:
        for line in fh:
            bundle = json.loads(line)
            for entry in bundle.get("entry", []):
                r = entry["resource"]
                lu = last_updated(r)
                if since and lu and lu <= since:
                    # what a real _since export would not have returned
                    counts["skipped-old"] += 1
                    continue
                kind, h, lu, late = classify(con, r, closed_periods)
                counts[kind] += 1
                if remember:
                    # Record the version so a LATER scan can tell a real change
                    # from a no-op touch. Without this every scan sees an empty
                    # resource_version table and reports everything as 'new',
                    # which makes the unchanged-touch counter permanently zero
                    # and the defence it represents untested.
                    record(con, r, kind, h, lu, now)
                if late:
                    lates.append(late)
                if lu and (max_lu is None or lu > max_lu):
                    max_lu = lu
    return {"counts": counts, "late_arrivals": lates, "new_watermark": max_lu,
            "scanned_at": now}


def commit_scan(con, result, source="bulk-export", now=None):
    now = now or result["scanned_at"]
    for late in result["late_arrivals"]:
        con.execute(
            "INSERT INTO late_arrival VALUES (?,?,?,?,?,?,?)",
            (late["resource_type"], late["resource_id"], late["clinical_date"],
             late["last_updated"], late["lag_days"], late["closed_period"], now))
    if result["new_watermark"]:
        total = sum(result["counts"][k] for k in ("new", "changed"))
        con.execute(
            "INSERT INTO load_watermark VALUES (?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET last_updated=excluded.last_updated,"
            " loaded_at=excluded.loaded_at, n_resources=excluded.n_resources",
            (source, result["new_watermark"], now, total))
    con.commit()


def record_run(con, run_id, measure, period, denominator, numerator,
               is_final=False, now=None):
    now = now or datetime.now().isoformat(timespec="seconds")
    rate = (numerator / denominator) if denominator else 0.0
    con.execute("INSERT OR REPLACE INTO measure_run VALUES (?,?,?,?,?,?,?,?)",
                (run_id, now, measure, period, denominator, numerator, rate,
                 int(is_final)))
    con.commit()
    return rate


def restatements(con, current_run_id, tolerance=0.0):
    """Has a previously-reported rate changed?

    Compares the current run against the most recent FINAL run for the same
    measure and period. `is_final` marks a run that was submitted or published;
    comparing against every prior run would flag ordinary intra-period movement
    as a restatement, which is not what the word means.

    The magnitude matters as much as the fact. A restatement of 0.1pp is a
    footnote; one that crosses a contractual threshold is a conversation with a
    health plan, and this returns the number rather than a boolean.
    """
    out = []
    finals = con.execute(
        "SELECT measure, period, denominator, numerator, rate, run_id, run_at "
        "FROM measure_run WHERE is_final=1 ORDER BY run_at").fetchall()
    latest_final = {}
    for m, p, d, n, r, rid, at in finals:
        latest_final[(m, p)] = (d, n, r, rid, at)

    current = con.execute(
        "SELECT measure, period, denominator, numerator, rate FROM measure_run "
        "WHERE run_id=?", (current_run_id,)).fetchall()
    for m, p, d, n, r in current:
        prior = latest_final.get((m, p))
        if not prior:
            continue
        pd_, pn, pr, prid, pat = prior
        if abs(r - pr) <= tolerance:
            continue
        out.append({
            "measure": m, "period": p,
            "reported_rate": pr, "current_rate": r,
            "delta_pp": (r - pr) * 100,
            "reported_denominator": pd_, "current_denominator": d,
            "reported_numerator": pn, "current_numerator": n,
            "reported_in_run": prid, "reported_at": pat,
            "driver": ("denominator grew" if d > pd_ else
                       "denominator shrank" if d < pd_ else
                       "numerator only -- members already in the denominator "
                       "became compliant"),
        })
    return out
