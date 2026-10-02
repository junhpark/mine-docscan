import pytest

from minedocscan.store.db import PRIMARY_KEYS, open_db, upsert


def test_schema_has_every_registered_table():
    con = open_db("sqlite:///:memory:")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(PRIMARY_KEYS) <= tables


def test_upsert_is_idempotent_and_updates():
    con = open_db("sqlite:///:memory:")
    row = {"work_date": "2030-01-07", "slot": "T01", "vehicle_no": "V-101", "operator": "ALPHA",
           "header_vehicle_no": "V-101", "header_operator": "ALPHA", "matched_by": "operator", "header_mismatch": 0}
    upsert(con, "eq_assignment_obs", row)
    upsert(con, "eq_assignment_obs", {**row, "vehicle_no": "V-909", "header_mismatch": 1})
    rows = con.execute("SELECT vehicle_no, header_mismatch FROM eq_assignment_obs").fetchall()
    assert [tuple(r) for r in rows] == [("V-909", 1)]
    assert upsert(con, "eq_assignment_obs", []) == 0


def test_only_sqlite_for_now():
    with pytest.raises(NotImplementedError):
        open_db("postgresql://localhost/mine")
