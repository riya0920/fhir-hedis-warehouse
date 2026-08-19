"""Flatten FHIR bundles into a relational warehouse, and define value sets as data.

FLATTENING PRINCIPLE
--------------------
**A code without its system is meaningless.** `44054006` is diabetes in SNOMED
CT and is a completely different thing in any other code system, or in none.
Every code column in this warehouse is stored as a (system, code, display)
triple, and there is a test that fails if a code ever lands without its system.

That single rule is most of what separates a FHIR warehouse from
`json_normalize(bundle)`.

WHAT IS LOST, DELIBERATELY -- see docs/FLATTENING.md for the full list
---------------------------------------------------------------------
Flattening is lossy and the honest move is to enumerate what was dropped, not
to imply the relational shape is equivalent:

  * only the FIRST coding of each CodeableConcept is stored. Real resources
    carry several (a SNOMED code and the local EHR code, say), and dropping the
    others loses the local code that a site's own analysts use.
  * extensions are dropped entirely, including US Core race and ethnicity --
    which means no disparity analysis is possible on this warehouse at all.
  * Provenance, narrative text, and contained resources are dropped.
  * references are stored as bare ids; conditional and absolute references
    would break.

dbt is not installed, so the "models" are SQL strings executed in dependency
order against SQLite by `build()`. The shape mirrors a dbt project (staging ->
marts -> measures, value sets as seeds) but there is no ref() graph, no
incremental materialisation, no lineage and no docs site.
"""

from __future__ import annotations

import json
import sqlite3

SCHEMA = """
DROP TABLE IF EXISTS patient;
CREATE TABLE patient (
    patient_id   TEXT PRIMARY KEY,
    member_id    TEXT,
    birth_date   TEXT,
    gender       TEXT
);
DROP TABLE IF EXISTS coverage;
CREATE TABLE coverage (
    patient_id   TEXT,
    span_start   TEXT,
    span_end     TEXT
);
DROP TABLE IF EXISTS condition;
CREATE TABLE condition (
    condition_id TEXT PRIMARY KEY,
    patient_id   TEXT,
    code_system  TEXT NOT NULL,
    code         TEXT NOT NULL,
    display      TEXT,
    onset_date   TEXT,
    clinical_status     TEXT,
    verification_status TEXT
);
DROP TABLE IF EXISTS observation;
CREATE TABLE observation (
    observation_id TEXT PRIMARY KEY,
    patient_id     TEXT,
    code_system    TEXT NOT NULL,
    code           TEXT NOT NULL,
    display        TEXT,
    effective_date TEXT,
    value_num      REAL,
    value_unit     TEXT,
    status         TEXT
);
DROP TABLE IF EXISTS procedure;
CREATE TABLE procedure (
    procedure_id TEXT PRIMARY KEY,
    patient_id   TEXT,
    code_system  TEXT NOT NULL,
    code         TEXT NOT NULL,
    display      TEXT,
    performed_date TEXT,
    status       TEXT
);
DROP TABLE IF EXISTS encounter;
CREATE TABLE encounter (
    encounter_id TEXT PRIMARY KEY,
    patient_id   TEXT,
    code_system  TEXT,
    code         TEXT,
    display      TEXT,
    start_date   TEXT,
    end_date     TEXT,
    class_code   TEXT
);
DROP TABLE IF EXISTS immunization;
CREATE TABLE immunization (
    immunization_id TEXT PRIMARY KEY,
    patient_id      TEXT,
    code_system     TEXT NOT NULL,
    code            TEXT NOT NULL,
    display         TEXT,
    occurrence_date TEXT,
    status          TEXT
);
DROP TABLE IF EXISTS value_set;
CREATE TABLE value_set (
    value_set_id  TEXT,
    value_set_name TEXT,
    code_system   TEXT,
    code          TEXT,
    display       TEXT
);
CREATE INDEX idx_cond_pat ON condition(patient_id);
CREATE INDEX idx_obs_pat  ON observation(patient_id);
CREATE INDEX idx_proc_pat ON procedure(patient_id);
CREATE INDEX idx_enc_pat  ON encounter(patient_id);
CREATE INDEX idx_imm_pat  ON immunization(patient_id);
CREATE INDEX idx_cov_pat  ON coverage(patient_id);
"""

# ---------------------------------------------------------------------------
# Value sets AS DATA, never inline code lists.
#
# This mirrors how VSAC works in production: measures reference a value set by
# OID, the value set is versioned and maintained centrally, and the measure SQL
# never contains a literal code. The reason is not tidiness -- it is that a
# value set changes (a new LOINC for the same assay, an ICD-10 revision) and
# when it does you want to change one seed row, not grep every measure.
#
# OIDs here are ILLUSTRATIVE placeholders, not real VSAC OIDs.
# ---------------------------------------------------------------------------
VALUE_SETS = {
    "2.16.840.1.113883.3.464.1003.103.12.1001": ("Diabetes", [
        ("http://snomed.info/sct", "44054006", "Diabetes mellitus type 2"),
        ("http://snomed.info/sct", "46635009", "Diabetes mellitus type 1"),
        ("http://hl7.org/fhir/sid/icd-10-cm", "E11.9", "Type 2 DM without complications"),
        ("http://hl7.org/fhir/sid/icd-10-cm", "E10.9", "Type 1 DM without complications"),
    ]),
    "2.16.840.1.113883.3.464.1003.198.12.1013": ("HbA1c Laboratory Test", [
        ("http://loinc.org", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood"),
        ("http://loinc.org", "17856-6", "Hemoglobin A1c in Blood by HPLC"),
        ("http://loinc.org", "4549-2", "Hemoglobin A1c/Hemoglobin.total in Blood by electrophoresis"),
    ]),
    "2.16.840.1.113883.3.464.1003.108.12.1018": ("Mammography", [
        ("http://www.ama-assn.org/go/cpt", "77067", "Screening mammography, bilateral"),
        ("http://www.ama-assn.org/go/cpt", "77066", "Diagnostic mammography, bilateral"),
        ("http://snomed.info/sct", "24623002", "Screening mammography"),
    ]),
    "2.16.840.1.113883.3.464.1003.198.12.1005": ("Bilateral Mastectomy", [
        ("http://snomed.info/sct", "428251008", "History of bilateral mastectomy"),
    ]),
    "2.16.840.1.113883.3.464.1003.1003": ("Hospice Encounter", [
        ("http://snomed.info/sct", "170935008", "Hospice care"),
    ]),
    "2.16.840.1.113883.3.464.1003.196.12.1214": ("DTaP Vaccine", [
        ("http://hl7.org/fhir/sid/cvx", "20", "DTaP"),
        ("http://hl7.org/fhir/sid/cvx", "106", "DTaP, 5 pertussis antigens"),
    ]),
}


def _first_coding(cc):
    """Return (system, code, display) from a CodeableConcept.

    Only the FIRST coding is kept. That is a documented loss, not an oversight:
    see docs/FLATTENING.md. A resource carrying both a SNOMED code and a local
    EHR code loses the local one here.
    """
    if not cc:
        return (None, None, None)
    codings = cc.get("coding") or []
    if not codings:
        return (None, None, cc.get("text"))
    c = codings[0]
    return (c.get("system"), c.get("code"), c.get("display") or cc.get("text"))


def _ref_id(ref):
    """'Patient/P000123' -> 'P000123'. Absolute and conditional references
    would break here and are not present in this data."""
    if not ref:
        return None
    return str(ref.get("reference", "")).split("/")[-1] or None


def _status_code(field):
    if not field:
        return None
    codings = field.get("coding") or []
    return codings[0].get("code") if codings else None


def load(ndjson_path, db_path="warehouse.db"):
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    rows = {k: [] for k in ("patient", "coverage", "condition", "observation",
                            "procedure", "encounter", "immunization")}

    with open(ndjson_path, encoding="utf-8") as fh:
        for line in fh:
            bundle = json.loads(line)
            for entry in bundle.get("entry", []):
                r = entry["resource"]
                rt = r["resourceType"]
                if rt == "Patient":
                    ident = (r.get("identifier") or [{}])[0].get("value")
                    rows["patient"].append((r["id"], ident, r.get("birthDate"),
                                            r.get("gender")))
                elif rt == "Coverage":
                    p = r.get("period", {})
                    rows["coverage"].append((_ref_id(r.get("beneficiary")),
                                             p.get("start"), p.get("end")))
                elif rt == "Condition":
                    s, c, d = _first_coding(r.get("code"))
                    rows["condition"].append((
                        r["id"], _ref_id(r.get("subject")), s, c, d,
                        r.get("onsetDateTime"),
                        _status_code(r.get("clinicalStatus")),
                        _status_code(r.get("verificationStatus"))))
                elif rt == "Observation":
                    s, c, d = _first_coding(r.get("code"))
                    vq = r.get("valueQuantity") or {}
                    rows["observation"].append((
                        r["id"], _ref_id(r.get("subject")), s, c, d,
                        r.get("effectiveDateTime"), vq.get("value"),
                        vq.get("unit"), r.get("status")))
                elif rt == "Procedure":
                    s, c, d = _first_coding(r.get("code"))
                    rows["procedure"].append((
                        r["id"], _ref_id(r.get("subject")), s, c, d,
                        r.get("performedDateTime"), r.get("status")))
                elif rt == "Encounter":
                    types = r.get("type") or [{}]
                    s, c, d = _first_coding(types[0])
                    p = r.get("period", {})
                    rows["encounter"].append((
                        r["id"], _ref_id(r.get("subject")), s, c, d,
                        p.get("start"), p.get("end"),
                        (r.get("class") or {}).get("code")))
                elif rt == "Immunization":
                    s, c, d = _first_coding(r.get("vaccineCode"))
                    rows["immunization"].append((
                        r["id"], _ref_id(r.get("patient")), s, c, d,
                        r.get("occurrenceDateTime"), r.get("status")))

    con.executemany("INSERT INTO patient VALUES (?,?,?,?)", rows["patient"])
    con.executemany("INSERT INTO coverage VALUES (?,?,?)", rows["coverage"])
    con.executemany("INSERT INTO condition VALUES (?,?,?,?,?,?,?,?)", rows["condition"])
    con.executemany("INSERT INTO observation VALUES (?,?,?,?,?,?,?,?,?)", rows["observation"])
    con.executemany("INSERT INTO procedure VALUES (?,?,?,?,?,?,?)", rows["procedure"])
    con.executemany("INSERT INTO encounter VALUES (?,?,?,?,?,?,?,?)", rows["encounter"])
    con.executemany("INSERT INTO immunization VALUES (?,?,?,?,?,?,?)", rows["immunization"])

    seeds = []
    for oid, (name, members) in VALUE_SETS.items():
        for system, code, display in members:
            seeds.append((oid, name, system, code, display))
    con.executemany("INSERT INTO value_set VALUES (?,?,?,?,?)", seeds)
    con.commit()
    return con, {k: len(v) for k, v in rows.items()}
