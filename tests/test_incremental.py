"""Tests for incremental load, late arrivals, and restatement.

The watermark tests are the ones that matter. Keying an incremental load on a
clinical date instead of meta.lastUpdated fails silently -- no error, no gap in
the row count, just a measure rate that is quietly too low forever -- so the
only way it gets caught is a test that constructs the case deliberately.
"""

import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import incremental as INC


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    INC.init(c)
    return c


def obs(rid, effective, value=7.0, lu=None):
    r = {"resourceType": "Observation", "id": rid, "status": "final",
         "subject": {"reference": "Patient/P1"},
         "effectiveDateTime": effective,
         "valueQuantity": {"value": value, "unit": "%"}}
    if lu:
        r["meta"] = {"lastUpdated": lu}
    return r


def _ndjson(tmp_path, resources, name="e.ndjson"):
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"resourceType": "Bundle", "type": "collection",
                             "entry": [{"resource": r} for r in resources]}) + "\n")
    return str(p)


# --------------------------------------------------------------------------
# the watermark
# --------------------------------------------------------------------------

def test_the_watermark_is_last_updated_not_the_clinical_date():
    r = obs("o1", "2024-01-15", lu="2024-11-20T00:00:00Z")
    assert INC.last_updated(r).startswith("2024-11-20")
    assert INC.clinical_date(r) == "2024-01-15"


def test_a_january_service_received_in_november_is_still_returned(con, tmp_path):
    """THE SILENT FAILURE. Keyed on the clinical date, a January service is
    behind a watermark that passed ten months ago and is never loaded. Keyed on
    lastUpdated it arrives in the November window, which is correct."""
    path = _ndjson(tmp_path, [obs("late", "2024-01-15", lu="2024-11-20T00:00:00Z")])
    got = INC.scan(path, con, since="2024-10-01T00:00:00Z")
    assert got["counts"]["new"] == 1
    assert got["counts"]["skipped-old"] == 0


def test_resources_at_or_before_the_watermark_are_not_returned(con, tmp_path):
    path = _ndjson(tmp_path, [obs("old", "2024-01-15", lu="2024-02-01T00:00:00Z")])
    got = INC.scan(path, con, since="2024-10-01T00:00:00Z")
    assert got["counts"]["skipped-old"] == 1
    assert got["counts"]["new"] == 0


def test_the_new_watermark_is_the_max_last_updated_seen(con, tmp_path):
    path = _ndjson(tmp_path, [
        obs("a", "2024-01-15", lu="2024-03-01T00:00:00Z"),
        obs("b", "2024-02-15", lu="2024-09-09T00:00:00Z"),
        obs("c", "2024-03-15", lu="2024-05-05T00:00:00Z")])
    got = INC.scan(path, con)
    assert got["new_watermark"].startswith("2024-09-09")


def test_clinical_date_is_read_per_resource_type():
    """A Procedure has no onsetDateTime and an Observation has no
    performedDateTime. A generic lookup returns None for half the corpus while
    looking like it worked."""
    assert INC.clinical_date(
        {"resourceType": "Procedure", "performedDateTime": "2024-04-01"}) \
        == "2024-04-01"
    assert INC.clinical_date(
        {"resourceType": "Condition", "onsetDateTime": "2019-01-01"}) \
        == "2019-01-01"
    assert INC.clinical_date(
        {"resourceType": "Immunization", "occurrenceDateTime": "2024-06-01"}) \
        == "2024-06-01"
    assert INC.clinical_date(
        {"resourceType": "Encounter", "period": {"start": "2024-07-01"}}) \
        == "2024-07-01"
    assert INC.clinical_date({"resourceType": "Patient"}) is None


# --------------------------------------------------------------------------
# the migration case
# --------------------------------------------------------------------------

def test_content_hash_ignores_meta():
    """The property the whole migration defence rests on."""
    a = obs("o1", "2024-01-15", lu="2024-02-01T00:00:00Z")
    b = obs("o1", "2024-01-15", lu="2025-07-01T00:00:00Z")
    assert INC.content_hash(a) == INC.content_hash(b)


def test_content_hash_changes_when_a_value_changes():
    a = obs("o1", "2024-01-15", value=7.0)
    b = obs("o1", "2024-01-15", value=9.4)
    assert INC.content_hash(a) != INC.content_hash(b)


def test_a_touched_resource_is_not_reported_as_changed(con, tmp_path):
    """A server re-index bumps lastUpdated on everything. A pipeline that
    treats 'returned by _since' as 'changed' would restate every measure it has
    ever published from a migration that changed no clinical fact."""
    first = _ndjson(tmp_path, [obs("o1", "2024-01-15", lu="2024-02-01T00:00:00Z")],
                    "a.ndjson")
    INC.scan(first, con, remember=True)
    con.commit()

    touched = _ndjson(tmp_path, [obs("o1", "2024-01-15", lu="2025-07-01T00:00:00Z")],
                      "b.ndjson")
    got = INC.scan(touched, con)
    assert got["counts"]["unchanged-touch"] == 1
    assert got["counts"]["changed"] == 0


def test_a_real_edit_is_reported_as_changed(con, tmp_path):
    first = _ndjson(tmp_path, [obs("o1", "2024-01-15", value=7.0,
                                   lu="2024-02-01T00:00:00Z")], "a.ndjson")
    INC.scan(first, con, remember=True)
    con.commit()
    edited = _ndjson(tmp_path, [obs("o1", "2024-01-15", value=9.4,
                                    lu="2025-07-01T00:00:00Z")], "b.ndjson")
    got = INC.scan(edited, con)
    assert got["counts"]["changed"] == 1


def test_a_touch_never_raises_a_late_arrival(con, tmp_path):
    """A migration must not manufacture late arrivals into a closed period."""
    first = _ndjson(tmp_path, [obs("o1", "2024-05-01", lu="2024-06-01T00:00:00Z")],
                    "a.ndjson")
    INC.scan(first, con, remember=True)
    con.commit()
    closed = (("MY2024", ("2024-01-01", "2024-12-31", "2025-01-30")),)
    touched = _ndjson(tmp_path, [obs("o1", "2024-05-01", lu="2025-07-01T00:00:00Z")],
                      "b.ndjson")
    got = INC.scan(touched, con, closed_periods=closed)
    assert got["late_arrivals"] == []


# --------------------------------------------------------------------------
# late arrivals
# --------------------------------------------------------------------------

def test_a_service_in_a_closed_period_arriving_after_close_is_late(con, tmp_path):
    closed = (("MY2024", ("2024-01-01", "2024-12-31", "2025-01-30")),)
    path = _ndjson(tmp_path, [obs("l", "2024-08-01", lu="2025-05-15T00:00:00Z")])
    got = INC.scan(path, con, closed_periods=closed)
    assert len(got["late_arrivals"]) == 1
    late = got["late_arrivals"][0]
    assert late["closed_period"] == "MY2024"
    assert late["lag_days"] == 287


def test_a_service_received_before_the_close_is_not_late(con, tmp_path):
    closed = (("MY2024", ("2024-01-01", "2024-12-31", "2025-01-30")),)
    path = _ndjson(tmp_path, [obs("x", "2024-08-01", lu="2024-09-01T00:00:00Z")])
    assert INC.scan(path, con, closed_periods=closed)["late_arrivals"] == []


def test_a_service_outside_the_closed_period_is_not_late(con, tmp_path):
    closed = (("MY2024", ("2024-01-01", "2024-12-31", "2025-01-30")),)
    path = _ndjson(tmp_path, [obs("y", "2025-03-01", lu="2025-05-01T00:00:00Z")])
    assert INC.scan(path, con, closed_periods=closed)["late_arrivals"] == []


# --------------------------------------------------------------------------
# restatement
# --------------------------------------------------------------------------

def test_a_rate_that_moves_after_a_final_run_is_a_restatement(con):
    INC.record_run(con, "r1", "CDC-A1C", "MY2024", 1370, 956,
                   is_final=True, now="2025-01-30")
    INC.record_run(con, "r2", "CDC-A1C", "MY2024", 1380, 996,
                   is_final=False, now="2025-06-30")
    rs = INC.restatements(con, "r2")
    assert len(rs) == 1
    assert rs[0]["delta_pp"] == pytest.approx(2.39, abs=0.02)
    assert rs[0]["driver"] == "denominator grew"


def test_a_numerator_only_change_is_named_as_such(con):
    """Different cause, different conversation: members already counted became
    compliant, rather than new members entering the measure."""
    INC.record_run(con, "r1", "BCS", "MY2024", 2300, 1400,
                   is_final=True, now="2025-01-30")
    INC.record_run(con, "r2", "BCS", "MY2024", 2300, 1460,
                   is_final=False, now="2025-06-30")
    assert "numerator only" in INC.restatements(con, "r2")[0]["driver"]


def test_an_unchanged_rate_is_not_a_restatement(con):
    INC.record_run(con, "r1", "BCS", "MY2024", 2300, 1400,
                   is_final=True, now="2025-01-30")
    INC.record_run(con, "r2", "BCS", "MY2024", 2300, 1400,
                   is_final=False, now="2025-06-30")
    assert INC.restatements(con, "r2") == []


def test_a_movement_inside_the_tolerance_is_not_a_restatement(con):
    INC.record_run(con, "r1", "BCS", "MY2024", 100000, 65000,
                   is_final=True, now="2025-01-30")
    INC.record_run(con, "r2", "BCS", "MY2024", 100000, 65010,
                   is_final=False, now="2025-06-30")
    assert INC.restatements(con, "r2", tolerance=0.0005) == []


def test_only_FINAL_runs_are_a_baseline(con):
    """Comparing against every prior run would flag ordinary intra-period
    movement as a restatement, which is not what the word means."""
    INC.record_run(con, "draft", "BCS", "MY2024", 2000, 1200,
                   is_final=False, now="2025-01-10")
    INC.record_run(con, "r2", "BCS", "MY2024", 2300, 1500,
                   is_final=False, now="2025-06-30")
    assert INC.restatements(con, "r2") == []


def test_a_measure_never_reported_cannot_be_restated(con):
    INC.record_run(con, "r2", "CIS-DTaP", "MY2024", 500, 400,
                   is_final=False, now="2025-06-30")
    assert INC.restatements(con, "r2") == []


def test_the_restatement_carries_the_run_it_is_measured_against(con):
    """An auditor asks 'against what', and a boolean cannot answer."""
    INC.record_run(con, "submission-feb", "BCS", "MY2024", 2300, 1492,
                   is_final=True, now="2025-01-30")
    INC.record_run(con, "r2", "BCS", "MY2024", 2312, 1519,
                   is_final=False, now="2025-06-30")
    r = INC.restatements(con, "r2")[0]
    assert r["reported_in_run"] == "submission-feb"
    assert r["reported_at"] == "2025-01-30"


# --------------------------------------------------------------------------
# watermark bookkeeping
# --------------------------------------------------------------------------

def test_the_watermark_is_persisted_and_read_back(con, tmp_path):
    path = _ndjson(tmp_path, [obs("a", "2024-01-15", lu="2024-09-09T00:00:00Z")])
    got = INC.scan(path, con)
    INC.commit_scan(con, got)
    assert INC.get_watermark(con).startswith("2024-09-09")


def test_committing_a_scan_persists_its_late_arrivals(con, tmp_path):
    closed = (("MY2024", ("2024-01-01", "2024-12-31", "2025-01-30")),)
    path = _ndjson(tmp_path, [obs("l", "2024-08-01", lu="2025-05-15T00:00:00Z")])
    INC.commit_scan(con, INC.scan(path, con, closed_periods=closed))
    n = con.execute("SELECT COUNT(*) FROM late_arrival").fetchone()[0]
    assert n == 1
