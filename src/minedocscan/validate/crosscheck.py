"""양식 간 교차검증.

운반 횟수는 차량별 일보(log)와 편×차량 행렬(matrix) 두 곳에 적힌다. 두 값이 다르면 둘 중 하나를
잘못 읽었거나 현장에서 잘못 적은 것이므로 검수 큐로 보낸다. 정답 데이터가 없어도 오류를 잡는 수단이다.

두 문서를 잇는 키는 행렬의 열 자리(slot)다. 행렬에 인쇄된 운전자·차량번호는 양식을 만든 당시 상태로
굳어 있어 실제와 다를 수 있으므로, 그날 일보에 손으로 적힌 (차량번호, 작성자)를 사실로 보고
  1) 작성자가 머리글 운전자와 같으면 그 자리
  2) 아니면 차량번호가 머리글 차량번호와 같은 자리
순서로 자리를 정한다. 그 결과는 eq_assignment_obs 에 남긴다 (그날의 실제 배차).

하루에 행렬 양식이 여러 장일 수 있으므로(상차 장비마다 한 장) 그날의 모든 장을 합쳐서 비교한다.

판정(status)은 최종 값(검수가 있으면 검수값)으로 한다. 기계가 읽은 횟수(trips_raw)의 합은 log_trips_raw,
matrix_trips_raw 에 같이 적어 인식기끼리 비교하는 일치율의 재료로 쓴다 (report.py).
dates 를 주면 그 날짜만 다시 계산한다 — 검수를 저장한 직후에 쓴다.
"""
from __future__ import annotations

import sqlite3

from ..store.db import upsert


def crosscheck_haul(con: sqlite3.Connection, exclude_materials: list[str] | None = None,
                    dates: list[str] | None = None) -> dict:
    exclude = set(exclude_materials or [])
    if dates is None:
        dates = [r[0] for r in con.execute("SELECT DISTINCT work_date FROM prod_haul WHERE work_date IS NOT NULL ORDER BY 1")]
    totals: dict[str, int] = {}
    for date in dates:
        for k, v in _crosscheck_date(con, date, exclude).items():
            totals[k] = totals.get(k, 0) + v
    con.commit()
    return totals


def _crosscheck_date(con: sqlite3.Connection, date: str, exclude: set[str]) -> dict:
    rows = con.execute("SELECT * FROM prod_haul WHERE work_date=?", (date,)).fetchall()
    matrix = [r for r in rows if r["source_role"] == "matrix" and r["material"] not in exclude]
    log = [r for r in rows if r["source_role"] == "log" and r["material"] not in exclude]

    slots: dict[str, tuple[str | None, str | None]] = {}       # slot → (머리글 운전자, 머리글 차량번호)
    for r in matrix:
        slots.setdefault(r["slot"], (r["operator"], r["vehicle_no"]))

    # ── 일보 한 장(page) → 자리 ──
    pages: dict[str, tuple[str | None, str | None]] = {}
    for r in log:
        pages.setdefault(r["page_id"], (r["vehicle_no"], r["operator"]))
    page_slot: dict[str, tuple[str, str]] = {}
    taken: set[str] = set()
    for pid, (_vehicle, operator) in pages.items():            # 1) 작성자 일치
        if not operator:
            continue
        for slot, (h_op, _h_veh) in slots.items():
            if slot not in taken and h_op and h_op == operator:
                page_slot[pid] = (slot, "operator")
                taken.add(slot)
                break
    for pid, (vehicle, _operator) in pages.items():            # 2) 차량번호 일치
        if pid in page_slot or not vehicle:
            continue
        for slot, (_h_op, h_veh) in slots.items():
            if slot not in taken and h_veh and str(h_veh) == str(vehicle):
                page_slot[pid] = (slot, "vehicle")
                taken.add(slot)
                break

    obs = []
    for pid, (slot, how) in page_slot.items():
        vehicle, operator = pages[pid]
        h_op, h_veh = slots[slot]
        con.execute("UPDATE prod_haul SET slot=? WHERE page_id=? AND source_role='log'", (slot, pid))
        obs.append({"work_date": date, "slot": slot, "vehicle_no": vehicle, "operator": operator,
                    "header_vehicle_no": h_veh, "header_operator": h_op, "matched_by": how,
                    "header_mismatch": int((h_op or "") != (operator or "") or str(h_veh or "") != str(vehicle or ""))})
    con.execute("DELETE FROM eq_assignment_obs WHERE work_date=?", (date,))
    upsert(con, "eq_assignment_obs", obs)

    # ── 값 모으기: (자리, 광종, 편) → 값 유무·횟수 ──
    m_has, m_trips, m_raw = _collect([(r["slot"], r) for r in matrix])
    resolved = [(page_slot[r["page_id"]][0], r) for r in log if r["page_id"] in page_slot]
    unresolved = [(f"unresolved:{pages[r['page_id']][0] or r['page_id']}", r) for r in log if r["page_id"] not in page_slot]
    l_has, l_trips, l_raw = _collect(resolved + unresolved)
    who = {slot: pages[pid] for pid, (slot, _how) in page_slot.items()}
    log_slots = {k[0] for k in l_has}

    out, counts = [], {}
    for key in sorted(set(m_has) | set(l_has)):
        slot, material, level = key
        lh, mh = l_has.get(key), m_has.get(key)
        if lh is None and slot not in log_slots:
            status = "missing_log"
        elif mh is None:
            status = "missing_matrix"
        else:
            lh = lh or 0
            lt, mt = l_trips.get(key), m_trips.get(key)
            same = lh == mh and (lt is None or mt is None or lt == mt)
            status = "match" if same else "mismatch"
        vehicle, operator = who.get(slot, (None, None))
        out.append({"work_date": date, "slot": slot, "material": material, "level": level, "operator": operator,
                    "vehicle_no": vehicle, "log_has": lh, "matrix_has": mh, "log_trips": l_trips.get(key),
                    "matrix_trips": m_trips.get(key), "log_trips_raw": l_raw.get(key),
                    "matrix_trips_raw": m_raw.get(key), "status": status})
        counts[status] = counts.get(status, 0) + 1
    con.execute("DELETE FROM xcheck_haul WHERE work_date=?", (date,))
    upsert(con, "xcheck_haul", out)
    return counts


def _collect(items) -> tuple[dict, dict, dict]:
    """같은 (자리, 광종, 편)에 여러 행(근무조·여러 장)이 있으면 값 유무는 OR, 횟수는 합 (최종값과 기계값 각각).

    값이 있는데 횟수를 모르는 행(has_value=1, trips NULL — 아직 검수하지 않았거나 인식기가 못 읽음)이 하나라도 섞이면
    그 칸의 합은 모르는 것으로 둔다(NULL → 값 유무만 비교). 주간만 검수하고 야간은 아직일 때 주간 값만을 행렬과
    비교해 가짜 불일치를 만들지 않기 위해서다.
    """
    has: dict[tuple, int] = {}
    trips: dict[tuple, int] = {}
    raw: dict[tuple, int] = {}
    unknown_final: set[tuple] = set()
    unknown_raw: set[tuple] = set()
    for slot, r in items:
        key = (slot, r["material"], r["level"])
        has[key] = max(has.get(key, 0), r["has_value"])
        if r["trips"] is not None:
            trips[key] = trips.get(key, 0) + r["trips"]
        elif r["has_value"]:
            unknown_final.add(key)
        if r["trips_raw"] is not None:
            raw[key] = raw.get(key, 0) + r["trips_raw"]
        elif r["has_value_raw"]:             # 기계가 본 대로: 기계가 값이 있다고 했는데 읽지 못한 칸
            unknown_raw.add(key)
    for k in unknown_final:
        trips.pop(k, None)
    for k in unknown_raw:
        raw.pop(k, None)
    return has, trips, raw
