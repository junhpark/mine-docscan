"""교차검증 논리만 따로 — 이미지 없이 prod_haul 행을 직접 넣어 본다."""
from minedocscan.store.db import open_db, upsert
from minedocscan.validate.crosscheck import crosscheck_haul

DAY = "2030-01-07"
HEADERS = {"T01": ("V-101", "ALPHA"), "T02": ("V-102", "BRAVO"), "T03": ("V-103", "CHARLIE")}


def _row(hid, role, page, slot, vehicle, operator, level, has, trips=None, material="ORE", shift=None):
    return {"haul_id": hid, "work_date": DAY, "source_form": role, "source_role": role, "page_id": page, "slot": slot,
            "vehicle_no": vehicle, "operator": operator, "material": material, "level": level, "shift": shift,
            "has_value_raw": has, "has_value": has, "trips": trips, "trips_raw": trips, "confidence": None,
            "source_field_id": None, "review_status": "auto"}


def _db(matrix: dict, logs: dict):
    """matrix: {(slot, level): trips|None}.  logs: {page: (vehicle, operator, {level: [(shift, trips|None)]})}"""
    con = open_db("sqlite:///:memory:")
    rows = []
    for slot, (veh, op) in HEADERS.items():
        for lv in ("L0", "L1"):
            t = matrix.get((slot, lv), 0)
            rows.append(_row(f"m:{slot}:{lv}", "matrix", "pm", slot, veh, op, lv, int(t != 0), t or None))
    for page, (veh, op, vals) in logs.items():
        for lv in ("L0", "L1"):
            for shift in ("day", "night"):
                t = dict(vals.get(lv, [])).get(shift, 0)        # 0 = 빈 칸, None = 값은 있는데 횟수 모름
                rows.append(_row(f"l:{page}:{lv}:{shift}", "log", page, None, veh, op, lv, int(t != 0), t or None,
                                 shift=shift))
    upsert(con, "prod_haul", rows)
    return con


def _status(con):
    return {(r["slot"], r["level"]): r["status"] for r in con.execute("SELECT * FROM xcheck_haul")}


def test_match_and_mismatch_on_presence():
    con = _db({("T01", "L0"): None, ("T02", "L1"): None},                      # None = 값은 있는데 숫자는 못 읽음
              {"p1": ("V-101", "ALPHA", {"L0": [("day", None)]}),
               "p2": ("V-102", "BRAVO", {"L0": [("day", None)]}),               # 일보는 L0, 행렬은 L1 → 둘 다 불일치
               "p3": ("V-103", "CHARLIE", {})})
    counts = crosscheck_haul(con)
    st = _status(con)
    assert st[("T01", "L0")] == "match" and st[("T01", "L1")] == "match"
    assert st[("T02", "L0")] == "mismatch" and st[("T02", "L1")] == "mismatch"
    assert counts == {"match": 4, "mismatch": 2}


def test_trips_are_summed_over_shifts():
    con = _db({("T01", "L0"): 7, ("T01", "L1"): 4},
              {"p1": ("V-101", "ALPHA", {"L0": [("day", 5), ("night", 2)], "L1": [("day", 3)]})})
    crosscheck_haul(con)
    st = _status(con)
    assert st[("T01", "L0")] == "match"            # 5 + 2 = 7
    assert st[("T01", "L1")] == "mismatch"         # 3 ≠ 4
    assert st[("T02", "L0")] == "missing_log"      # 일보를 내지 않은 차량


def test_slot_resolution_operator_first_then_vehicle():
    con = _db({("T01", "L0"): 1, ("T02", "L0"): 1, ("T03", "L0"): 1},
              {"p1": ("V-101", "ALPHA", {"L0": [("day", 1)]}),                  # 머리글 그대로
               "p2": ("V-909", "BRAVO", {"L0": [("day", 1)]}),                  # 같은 운전자, 다른 차
               "p3": ("V-103", "ECHO", {"L0": [("day", 1)]})})                  # 같은 차, 다른 운전자
    counts = crosscheck_haul(con)
    obs = {r["slot"]: (r["vehicle_no"], r["operator"], r["matched_by"], r["header_mismatch"])
           for r in con.execute("SELECT * FROM eq_assignment_obs")}
    assert obs == {"T01": ("V-101", "ALPHA", "operator", 0),
                   "T02": ("V-909", "BRAVO", "operator", 1),
                   "T03": ("V-103", "ECHO", "vehicle", 1)}
    assert counts == {"match": 6}
    assert {r[0] for r in con.execute("SELECT DISTINCT slot FROM prod_haul WHERE source_role='log'")} == set(HEADERS)


def test_unresolved_log_is_reported_not_dropped():
    con = _db({("T01", "L0"): 1}, {"p9": ("V-777", "ZULU", {"L0": [("day", 1)]})})
    crosscheck_haul(con)
    st = _status(con)
    assert st[("unresolved:V-777", "L0")] == "missing_matrix"
    assert st[("T01", "L0")] == "missing_log"


def test_excluded_material_and_rerun_is_idempotent():
    con = _db({("T01", "L0"): 1}, {"p1": ("V-101", "ALPHA", {"L0": [("day", 1)]})})
    upsert(con, "prod_haul", _row("l:p1:surface", "log", "p1", None, "V-101", "ALPHA", "-", 1, 3, material="SURFACE"))
    first = crosscheck_haul(con, exclude_materials=["SURFACE"])
    assert "missing_matrix" not in first
    assert crosscheck_haul(con, exclude_materials=["SURFACE"]) == first
    assert con.execute("SELECT COUNT(*) FROM xcheck_haul").fetchone()[0] == sum(first.values())


def test_partial_counts_compare_presence_only():
    """주간은 검수(2)했고 야간은 아직(값은 있는데 횟수 모름)이면 2 와 행렬 4 를 비교하지 않는다 — 값 유무만 본다."""
    con = _db({("T01", "L0"): 4},
              {"p1": ("V-101", "ALPHA", {"L0": [("day", 2), ("night", None)]})})
    crosscheck_haul(con)
    row = con.execute("SELECT status, log_trips, matrix_trips FROM xcheck_haul WHERE slot='T01' AND level='L0'").fetchone()
    assert tuple(row) == ("match", None, 4)
    # 야간까지 검수하면 횟수로 비교한다
    con.execute("UPDATE prod_haul SET trips=2 WHERE haul_id='l:p1:L0:night'")
    crosscheck_haul(con)
    assert con.execute("SELECT status FROM xcheck_haul WHERE slot='T01' AND level='L0'").fetchone()[0] == "match"
    con.execute("UPDATE prod_haul SET trips=3 WHERE haul_id='l:p1:L0:night'")
    crosscheck_haul(con)
    assert con.execute("SELECT status FROM xcheck_haul WHERE slot='T01' AND level='L0'").fetchone()[0] == "mismatch"


def test_machine_sum_follows_machine_presence():
    """기계가 값을 놓친 셀(has_value_raw=0)을 사람이 value 로 검수해도, 기계 값 합은 기계가 본 대로 계산된다 — "모름"이 아니다."""
    con = _db({("T01", "L0"): 4}, {"p1": ("V-101", "ALPHA", {"L0": [("day", 4)]})})
    # 야간 칸: 기계는 빈 칸이라 했는데(has_value_raw 0, trips_raw NULL) 사람이 1 을 적었다 (has_value 1, trips 1)
    con.execute("UPDATE prod_haul SET has_value=1, trips=1 WHERE haul_id='l:p1:L0:night'")
    crosscheck_haul(con)
    row = con.execute("SELECT status, log_trips, log_trips_raw FROM xcheck_haul WHERE slot='T01' AND level='L0'").fetchone()
    assert tuple(row) == ("mismatch", 5, 4)            # 최종 5 ≠ 4 → 불일치. 기계 값 합은 4 (모름이 아니다)
