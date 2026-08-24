"""The dbt models and `src/measures.py` must agree MEMBER FOR MEMBER.

WHY THIS IS THE POINT OF THE dbt PROJECT
-----------------------------------------
The dbt models are not a port of `src/measures.py`; they are a genuinely
independent reimplementation. The Python walks enrolment spans with a cursor
over Python `date` objects and accumulates sets; the SQL does it with a running
maximum in a window function and boolean columns. Different language, different
data model, different failure modes.

Two implementations that agree are much stronger evidence than one that passes
its own tests -- the same discipline the rest of this portfolio uses when it
differences a hand-rolled estimator against `lifelines` or `pydicom`.

AND IT IS CHECKED MEMBER FOR MEMBER, NOT RATE FOR RATE
--------------------------------------------------------
Comparing headline rates would be much weaker. Two implementations can produce
the same rate while disagreeing about WHICH members qualify -- one wrongly
including a member and wrongly excluding another cancels out perfectly in the
ratio. The symmetric difference of the member sets cannot cancel.

SKIPS when duckdb, the warehouse, or the dbt build is absent.
"""

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import measures as M

duckdb = pytest.importorskip("duckdb", reason="dbt parity audit only")

WAREHOUSE = os.path.join(ROOT, "warehouse.db")
DUCK = os.path.join(ROOT, "dbt", "hedis.duckdb")

FCT = {"CDC-A1C": "fct_cdc_a1c", "BCS": "fct_bcs", "CIS-DTaP": "fct_cis_dtap"}

pytestmark = pytest.mark.skipif(
    not (os.path.exists(WAREHOUSE) and os.path.exists(DUCK)),
    reason="run `python run_dbt.py` once to build the dbt models")


@pytest.fixture(scope="module")
def cons():
    sq = sqlite3.connect(WAREHOUSE)
    dk = duckdb.connect(DUCK, read_only=True)
    yield sq, dk
    sq.close()
    dk.close()


def _dbt_sets(dk, table):
    den = {r[0] for r in dk.execute(
        "select patient_id from %s where in_denominator" % table).fetchall()}
    num = {r[0] for r in dk.execute(
        "select patient_id from %s where in_numerator" % table).fetchall()}
    return den, num


def _py_sets(sq, name):
    r = M.MEASURES[name](sq)
    return set(r.denominator_ids), set(r.numerator_ids)


@pytest.mark.parametrize("measure", sorted(FCT))
def test_denominator_matches_member_for_member(cons, measure):
    sq, dk = cons
    py_den, _ = _py_sets(sq, measure)
    db_den, _ = _dbt_sets(dk, FCT[measure])
    assert py_den == db_den, (
        "%d members differ: %s" % (len(py_den ^ db_den),
                                   sorted(py_den ^ db_den)[:5]))


@pytest.mark.parametrize("measure", sorted(FCT))
def test_numerator_matches_member_for_member(cons, measure):
    sq, dk = cons
    _, py_num = _py_sets(sq, measure)
    _, db_num = _dbt_sets(dk, FCT[measure])
    assert py_num == db_num, (
        "%d members differ: %s" % (len(py_num ^ db_num),
                                   sorted(py_num ^ db_num)[:5]))


def test_the_denominators_are_not_trivially_empty(cons):
    """A parity test between two empty sets passes and proves nothing.

    This is the same discipline as making a control FIRE before believing it:
    the comparison above is only evidence if there is something to compare.
    """
    sq, dk = cons
    for measure in FCT:
        py_den, py_num = _py_sets(sq, measure)
        assert len(py_den) > 100, measure
        assert len(py_num) > 50, measure


def test_the_comparison_would_notice_a_difference(cons):
    """PROVES THE COMPARATOR DISCRIMINATES.

    Compare one measure's Python denominator against a DIFFERENT measure's dbt
    denominator. These must not match -- if they did, the comparison above
    would be passing for a reason that has nothing to do with correctness.
    """
    sq, dk = cons
    py_den, _ = _py_sets(sq, "CDC-A1C")
    wrong_den, _ = _dbt_sets(dk, FCT["BCS"])
    assert py_den != wrong_den


def test_the_rate_agrees_too(cons):
    """The headline number, as a cross-check on the aggregation rather than on
    the member logic -- the member-set tests above are the real evidence."""
    sq, dk = cons
    rows = dk.execute(
        "select measure, denominator, numerator from mart_measure_rates"
    ).fetchall()
    got = {m: (d, n) for m, d, n in rows}
    for measure in FCT:
        py_den, py_num = _py_sets(sq, measure)
        assert got[measure] == (len(py_den), len(py_num))


def test_enrolment_is_not_all_true(cons):
    """The continuous-enrolment gate must actually EXCLUDE somebody.

    If every member passed it, the SQL walk could be returning a constant and
    all three measures would still agree with a Python implementation that had
    the same bug.
    """
    dk = cons[1]
    total, cont = dk.execute(
        "select count(*), count(*) filter (where is_continuous) "
        "from int_enrolment").fetchone()
    assert total > 1000
    assert 0 < cont < total, "the enrolment gate excluded nobody"
