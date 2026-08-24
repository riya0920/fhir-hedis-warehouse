"""Run the dbt project against the SQLite warehouse.

WHY dbt IS HERE AT ALL
----------------------
The Python measures in `src/measures.py` are correct and tested, and they are
also a single 350-line module that computes three rates. That is fine until
somebody asks the questions a health plan actually asks:

  * which model does the age band live in, and what else depends on it?
  * if the continuous-enrolment rule changes, what breaks?
  * where is the test that says a numerator cannot exceed its denominator?

Those questions are about a GRAPH, and `src/measures.py` does not have one. dbt
is the answer to them: `ref()` makes the dependency graph explicit and
executable, materialisation makes the intermediate results inspectable instead
of trapped in a Python dict, and the schema tests turn assertions that were
prose in a docstring into things that fail a build.

WHY IT IS A SECOND IMPLEMENTATION AND NOT A REPLACEMENT
--------------------------------------------------------
`src/measures.py` stays. The dbt models are a genuinely independent
reimplementation of the same three measures, and `tests/test_dbt_parity.py`
asserts the two agree **member for member** -- not just on the headline rate,
which could match by luck while both are wrong about which members qualify.

Two implementations that agree are much stronger evidence than one that passes
its own tests, and it is the same discipline the rest of this portfolio uses
when it differences a hand-rolled estimator against a reference.

duckdb reads the SQLite warehouse in place through the `sqlite` extension.
Nothing is copied and nothing is written back to it.

Run:  python run_dbt.py            # build, then test
      python run_dbt.py --select int_enrolment
      python run_dbt.py docs       # generate the docs site into dbt/target
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DBT_DIR = os.path.join(ROOT, "dbt")
WAREHOUSE = os.path.join(ROOT, "warehouse.db")
DUCKDB = os.path.join(DBT_DIR, "hedis.duckdb")


def _env():
    if not os.path.exists(WAREHOUSE):
        raise SystemExit(
            "no warehouse at %s -- build it first with the load step in the "
            "README (python -c \"import src.warehouse as w; "
            "w.load('data/bundles.ndjson')\")" % WAREHOUSE)
    os.environ["SQLITE_WAREHOUSE"] = WAREHOUSE.replace("\\", "/")
    os.environ["DUCKDB_PATH"] = DUCKDB.replace("\\", "/")
    os.environ["DBT_PROFILES_DIR"] = DBT_DIR


def run(argv=None):
    """Invoke dbt in-process. Returns the exit code."""
    _env()
    from dbt.cli.main import dbtRunner

    argv = list(argv or [])
    argv += ["--project-dir", DBT_DIR, "--profiles-dir", DBT_DIR]
    result = dbtRunner().invoke(argv)
    return 0 if result.success else 1


def build_and_test(quiet=False):
    """`dbt build` runs models and their tests in dependency order.

    Using `build` rather than `run` then `test` matters: `build` will not run a
    model whose upstream TEST failed, so a broken assumption stops the graph
    instead of silently feeding a mart.
    """
    args = ["build"]
    if quiet:
        args.append("--quiet")
    return run(args)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "docs":
        code = run(["docs", "generate"])
        if code == 0:
            print("docs written to %s" % os.path.join(DBT_DIR, "target"))
        return code
    if argv:
        return run(["build"] + argv)
    return build_and_test()


if __name__ == "__main__":
    raise SystemExit(main())
