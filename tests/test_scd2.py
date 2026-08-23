"""Tests for SCD2 dimension history and the fact merge.

The point-in-time tests are what make a published disparity finding
defensible. A stratified rate is reproducible only if BOTH the denominator and
its attributes can be reconstructed, and an overwriting warehouse loses the
second one silently.
"""

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import scd2


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:", isolation_level=None)
    return scd2.init(c)


WHITE = {"birth_date": "1960-01-01", "gender": "female",
         "race_code": "2106-3", "race_display": "White",
         "ethnicity_code": "2186-5", "ethnicity_display": "Not Hispanic"}
BLACK = dict(WHITE, race_code="2054-5", race_display="Black or African American")
UNKNOWN = dict(WHITE, race_code=None, race_display=None)


def test_a_first_load_inserts_version_one(con):
    assert scd2.upsert(con, "p1", WHITE, "2024-01-01") == "inserted"
    cur = scd2.current(con, "p1")
    assert cur["version"] == 1 and cur["is_current"] == 1
    assert cur["valid_to"] is None


def test_a_no_op_update_creates_no_version(con):
    """FHIR's meta.lastUpdated moves on ANY write, so without this a server
    re-index produces a new version per patient per migration and the history
    becomes noise that hides the real changes inside it."""
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    assert scd2.upsert(con, "p1", dict(WHITE), "2025-07-01") == "unchanged"
    assert len(scd2.history(con, "p1")) == 1


def test_a_real_change_versions_rather_than_overwrites(con):
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    assert scd2.upsert(con, "p1", BLACK, "2025-03-01") == "versioned"
    hist = scd2.history(con, "p1")
    assert len(hist) == 2
    assert hist[0]["is_current"] == 0 and hist[1]["is_current"] == 1
    assert hist[0]["race_code"] == "2106-3"      # the old value SURVIVES


def test_the_change_reason_names_what_moved(con):
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    scd2.upsert(con, "p1", BLACK, "2025-03-01")
    assert "race_code" in scd2.history(con, "p1")[1]["change_reason"]


def test_a_point_in_time_query_returns_the_belief_of_that_date(con):
    """The function the module exists for: what did we believe on the day the
    February report was published?"""
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    scd2.upsert(con, "p1", BLACK, "2025-03-01")
    assert scd2.as_of(con, "p1", "2025-01-30")["race_code"] == "2106-3"
    assert scd2.as_of(con, "p1", "2025-06-30")["race_code"] == "2054-5"


def test_the_valid_to_bound_is_exclusive(con):
    """Two changes on the same day is exactly when a data-quality project is
    running. An inclusive bound set to 'the day before' breaks there."""
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    scd2.upsert(con, "p1", BLACK, "2025-03-01")
    scd2.upsert(con, "p1", UNKNOWN, "2025-03-01")     # same day
    rows = [scd2.as_of(con, "p1", d)
            for d in ("2024-06-01", "2025-03-01", "2025-04-01")]
    assert rows[0]["race_code"] == "2106-3"
    # exactly one row is returned at the boundary, and it is the latest
    assert rows[1]["race_code"] is None
    assert rows[2]["race_code"] is None


def test_a_query_before_the_first_version_returns_nothing(con):
    """Not a guess. A patient we had never seen has no attributes, and
    inventing 'unknown' would put them in a stratum."""
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    assert scd2.as_of(con, "p1", "2023-01-01") is None


def test_the_stratification_can_be_reconstructed_as_it_stood(con):
    """A disparity finding that cannot be reproduced cannot be defended."""
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    scd2.upsert(con, "p2", BLACK, "2024-01-01")
    scd2.upsert(con, "p3", UNKNOWN, "2024-01-01")
    scd2.upsert(con, "p3", BLACK, "2025-04-01")       # unknown -> recorded

    feb = scd2.stratum_as_of(con, ["p1", "p2", "p3"], "2025-01-30")
    jun = scd2.stratum_as_of(con, ["p1", "p2", "p3"], "2025-06-30")
    assert feb["p3"] is None and jun["p3"] == "2054-5"
    assert feb["p1"] == jun["p1"]


def test_drift_reports_the_movements_not_just_a_count(con):
    """'3 patients moved between race categories' and '3 moved from unknown to
    a recorded value' are different events -- the second shrinks an unknown
    bucket that was suppressing the gap."""
    for pid in ("p1", "p2", "p3"):
        scd2.upsert(con, pid, UNKNOWN, "2024-01-01")
    scd2.upsert(con, "p1", BLACK, "2025-04-01")
    scd2.upsert(con, "p2", BLACK, "2025-04-01")
    d = scd2.drift(con, ["p1", "p2", "p3"], "2025-01-30", "2025-06-30")
    assert d["n_changed"] == 2
    assert d["movements"][0]["from"] is None
    assert d["movements"][0]["to"] == "2054-5"
    assert d["movements"][0]["n"] == 2


def test_no_drift_when_nothing_changed(con):
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    d = scd2.drift(con, ["p1"], "2025-01-30", "2025-06-30")
    assert d["n_changed"] == 0 and d["movements"] == []


def test_versions_are_numbered_consecutively(con):
    scd2.upsert(con, "p1", WHITE, "2024-01-01")
    scd2.upsert(con, "p1", BLACK, "2025-01-01")
    scd2.upsert(con, "p1", UNKNOWN, "2025-06-01")
    assert [h["version"] for h in scd2.history(con, "p1")] == [1, 2, 3]
    assert sum(h["is_current"] for h in scd2.history(con, "p1")) == 1


# --------------------------------------------------------------------------
# the fact merge
# --------------------------------------------------------------------------

@pytest.fixture
def facts():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE obs (id TEXT PRIMARY KEY, patient_id TEXT, "
              "code TEXT, value REAL)")
    return c


COLS = ["id", "patient_id", "code", "value"]


def test_a_merge_inserts_only_what_is_new(facts):
    stats = scd2.merge_facts(facts, "obs", [
        {"id": "o1", "patient_id": "p1", "code": "a1c", "value": 7.0}],
        ["id"], COLS)
    assert stats == {"inserted": 1, "updated": 0, "unchanged": 0}


def test_an_unchanged_row_is_not_touched(facts):
    """Not an optimisation. A merge that rewrites every row destroys the one
    signal an operator has -- how much actually changed last night -- and turns
    a 12-row delta into something indistinguishable from a corruption."""
    row = {"id": "o1", "patient_id": "p1", "code": "a1c", "value": 7.0}
    scd2.merge_facts(facts, "obs", [row], ["id"], COLS)
    stats = scd2.merge_facts(facts, "obs", [dict(row)], ["id"], COLS)
    assert stats == {"inserted": 0, "updated": 0, "unchanged": 1}


def test_a_changed_value_updates_in_place(facts):
    row = {"id": "o1", "patient_id": "p1", "code": "a1c", "value": 7.0}
    scd2.merge_facts(facts, "obs", [row], ["id"], COLS)
    stats = scd2.merge_facts(facts, "obs", [dict(row, value=9.4)], ["id"], COLS)
    assert stats == {"inserted": 0, "updated": 1, "unchanged": 0}
    assert facts.execute("SELECT value FROM obs WHERE id='o1'").fetchone()[0] \
        == 9.4


def test_a_merge_of_a_mixed_delta_reports_each_kind(facts):
    scd2.merge_facts(facts, "obs", [
        {"id": "o1", "patient_id": "p1", "code": "a1c", "value": 7.0},
        {"id": "o2", "patient_id": "p1", "code": "a1c", "value": 8.0}],
        ["id"], COLS)
    stats = scd2.merge_facts(facts, "obs", [
        {"id": "o1", "patient_id": "p1", "code": "a1c", "value": 7.0},
        {"id": "o2", "patient_id": "p1", "code": "a1c", "value": 8.5},
        {"id": "o3", "patient_id": "p2", "code": "a1c", "value": 6.1}],
        ["id"], COLS)
    assert stats == {"inserted": 1, "updated": 1, "unchanged": 1}
    assert facts.execute("SELECT COUNT(*) FROM obs").fetchone()[0] == 3
