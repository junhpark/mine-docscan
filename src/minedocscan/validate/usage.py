"""가동 일보의 검산 (tasks/0005 4.4) → xcheck_usage. 보여 주는 것이다 — 값을 고치지 않는다, 앞날의 종료로 오늘의 시작을 채우지 않는다
(ADR 0006). 어긋난 것은 검수 대기열(usage-check)에서 사람이 두 칸을 같이 본다.

  쪽 안   total       총 = 종료 − 시작 (셋 다 계기 값일 때)                        match | mismatch
          subtotal    소계 = 합 (작업량 표의 소계 칸 — 템플릿의 subtotal: true)    match | mismatch | unknown(모르는 칸이 있다)
  날짜 사이 continuity 같은 장비의, 계기 값이 있는 바로 앞 기록의 종료 = 이 기록의 시작
                      match | gap(시작이 더 크다 — 빠진 날이 있거나 잘못 적었다) | overlap(시작이 더 작다) | first(앞 기록 없음)
                      | unknown(장비를 모른다, 값이 검수 대기다). 차이(시간)와 사이에 낀 날 수를 같이 남긴다

같은 장비 = 장비 ID 가 있으면 ID, 없으면 적힌 이름이 같은 것 (equipment_ref: "id:<UUID>" | "name:<이름>"). 이름이 없는 쪽은 unknown.
기록의 순서: 날짜 → 계기 시작 값이 작은 쪽 → 문서 이름 → 쪽 번호 (하루 두 장 — tasks/0005 9절). 바로 앞 기록이 계기 칸을 아직
모르는(검수 대기) 기록이면 그보다 앞을 찾지 않고 unknown — 일부만 검수한 중에 가짜 gap 이 생기지 않게 (crosscheck 와 같은 생각).

마무리(finalize)에서 전체를, 검수를 저장하면 그 쪽의 쪽 안 검산과 그 장비(이름이 바뀌었으면 예전·새 장비 둘 다)의 연속성만
다시 계산한다. 연속성은 장비마다 그 장비의 기록만으로 정해지는 함수라 둘의 결과가 같다 (불변식).
"""
from __future__ import annotations

import sqlite3
from datetime import date

from ..forms.template import Template
from ..store.db import upsert

TOLERANCE = 0.05            # 계기 값의 허용 오차(시간): 소수 한 자리의 반올림 (tasks/0005 9절의 기본값)
KINDS = ("total", "subtotal", "continuity")
PAGE_KINDS = ("total", "subtotal")


def equipment_ref(row) -> str | None:
    """같은 장비를 가르는 기준: 장비 ID, 없으면 적힌 이름."""
    if row["equipment_id"]:
        return f"id:{row['equipment_id']}"
    return f"name:{row['equipment']}" if row["equipment"] else None


# ── 쪽 안 ──────────────────────────────────────────────────────────────────
def page_checks(con: sqlite3.Connection, tpl: Template | None, page_id: str) -> list[dict]:
    """그 쪽의 쪽 안 검산 행 (total, subtotal). eq_usage_daily·prod_tally·doc_field 의 최종 값에서."""
    u = con.execute("SELECT * FROM eq_usage_daily WHERE page_id = ?", (page_id,)).fetchone()
    if u is None:
        return []
    out = []
    s, e, t = u["meter_start"], u["meter_end"], u["meter_total"]
    if s is not None and e is not None and t is not None:
        d = round(t - (e - s), 4)
        out.append(_row(u, "total", "", t, round(e - s, 4), d, "match" if abs(d) <= TOLERANCE else "mismatch",
                        field_a=u["total_field_id"], field_b=u["end_field_id"]))
    if tpl is not None:
        out += _subtotal_checks(con, tpl, u)
    return out


def _subtotal_checks(con, tpl: Template, u) -> list[dict]:
    """작업량 표의 소계 칸: 소계 열이면 같은 행의 소계가 아닌 칸의 합, 소계 행이면 같은 열의 소계가 아닌 행의 합.
    소계 칸에 값이 있을 때만 (비어 있으면 검산할 것이 없다). 더할 칸에 모르는 칸(잉크는 있는데 값이 없다)이 있으면 unknown."""
    cells = {(r["region"], r["row_no"], r["field_name"]): r for r in con.execute(
        "SELECT region, row_no, field_name, field_id, has_value, value_final FROM doc_field WHERE page_id = ? "
        "AND format = 'integer'", (u["page_id"],))}
    out = []
    for reg in tpl.regions:
        if reg.get("role") != "tally":
            continue
        name = reg["name"]
        cols = [c for c in reg["columns"] if c.get("kind", "").startswith("handwritten") and (c.get("format") or "integer")
                == "integer"]
        sub_cols = [c["name"] for c in cols if c.get("subtotal")]
        val_cols = [c["name"] for c in cols if not c.get("subtotal")]
        sub_rows = [r["row"] for r in reg["rows"] if r.get("subtotal")]
        val_rows = [r["row"] for r in reg["rows"] if not r.get("subtotal")]
        pairs = [((row, sc), [(row, vc) for vc in val_cols]) for row in [*val_rows, *sub_rows] for sc in sub_cols]
        pairs += [((sr, vc), [(vr, vc) for vr in val_rows]) for sr in sub_rows for vc in val_cols]
        for (row, col), parts in pairs:
            cell = cells.get((name, row, col))
            if cell is None or not cell["has_value"]:
                continue
            got = _int(cell)
            vals = [_int(cells[(name, r, c)]) if (name, r, c) in cells else 0 for r, c in parts]
            if got is None or any(v is None for v in vals):
                out.append(_row(u, "subtotal", cell["field_id"], got, None, None, "unknown", field_a=cell["field_id"]))
                continue
            total = sum(vals)
            out.append(_row(u, "subtotal", cell["field_id"], got, total, got - total,
                            "match" if got == total else "mismatch", field_a=cell["field_id"]))
    return out


def _int(cell) -> int | None:
    """칸의 최종 정수: 빈 칸 0, 모르는 칸(잉크는 있는데 값이 없다) None."""
    if not cell["has_value"]:
        return 0
    v = cell["value_final"]
    return int(v) if v is not None and str(v).strip().isdigit() else None


# ── 날짜 사이: 계기의 연속성 ────────────────────────────────────────────────
def _records(con, where: str = "", args: tuple = ()) -> list[dict]:
    rows = con.execute(
        "SELECT u.*, d.source_name, p.page_no FROM eq_usage_daily u JOIN doc_page p ON u.page_id = p.page_id "
        f"JOIN doc_document d ON p.document_id = d.document_id {where}", args).fetchall()
    out = [dict(r) | {"ref": equipment_ref(r)} for r in rows]
    out.sort(key=_order)
    return out


def _order(r: dict) -> tuple:
    s = r["meter_start"]
    return (r["work_date"] or "", s is None, s if s is not None else 0.0, r["source_name"], r["page_no"])


def continuity_rows(records: list[dict]) -> list[dict]:
    """한 장비(같은 ref)의 기록들 → 연속성 검산 행. ref 가 None 인 기록은 하나씩 unknown."""
    out = []
    for i, r in enumerate(records):
        if r["meter_start"] is None and r["reading_kind"] != "pending":
            continue                                         # 계기 값이 없는 기록 (시각·빈 칸): 연속성 검산이 없다
        if r["ref"] is None or r["meter_start"] is None:     # 장비를 모른다, 시작 값이 검수 대기다
            out.append(_row(r, "continuity", "", r["meter_start"], None, None, "unknown", ref=r["ref"],
                            field_a=r["start_field_id"]))
            continue
        prev, blocked = None, False
        for q in reversed(records[:i]):
            if q["meter_end"] is not None:
                prev = q
                break
            if q["reading_kind"] == "pending":               # 앞 기록의 종료를 아직 모른다 — 더 앞을 보지 않는다
                blocked = True
                break
        if blocked:
            out.append(_row(r, "continuity", "", r["meter_start"], None, None, "unknown", ref=r["ref"],
                            field_a=r["start_field_id"]))
            continue
        if prev is None:
            out.append(_row(r, "continuity", "", r["meter_start"], None, None, "first", ref=r["ref"],
                            field_a=r["start_field_id"]))
            continue
        d = round(r["meter_start"] - prev["meter_end"], 4)
        result = "match" if abs(d) <= TOLERANCE else ("gap" if d > 0 else "overlap")
        out.append(_row(r, "continuity", "", r["meter_start"], prev["meter_end"], d, result, ref=r["ref"],
                        days=_days_between(prev["work_date"], r["work_date"]), other=prev["page_id"],
                        field_a=r["start_field_id"], field_b=prev["end_field_id"]))
    return out


def _days_between(a: str | None, b: str | None) -> int | None:
    """두 기록 사이에 낀 날의 수 (같은 날·이어진 날은 0)."""
    if not a or not b:
        return None
    try:
        return max(0, (date.fromisoformat(b) - date.fromisoformat(a)).days - 1)
    except ValueError:
        return None


def recompute_continuity(con: sqlite3.Connection, refs: set[str | None] | None = None, pages: set[str] = frozenset()) -> int:
    """연속성 검산을 다시 계산한다. refs=None 이면 전부. 아니면 그 장비들(과 pages 의 쪽 — 장비를 모르게 된 쪽 포함)만."""
    if refs is None:
        con.execute("DELETE FROM xcheck_usage WHERE check_kind = 'continuity'")
        records = _records(con)
    else:
        refs = {r for r in refs if r is not None}
        for col, vals in (("equipment_ref", sorted(refs)), ("page_id", sorted(pages))):   # 빈 IN () 은 PostgreSQL 이 받지 않는다
            if vals:
                con.execute(f"DELETE FROM xcheck_usage WHERE check_kind = 'continuity' AND {col} IN "
                            f"({','.join('?' * len(vals))})", vals)
        records = [r for r in _records(con) if r["ref"] in refs or (r["ref"] is None and r["page_id"] in pages)]
    groups: dict = {}
    for r in records:
        groups.setdefault(r["ref"], []).append(r)
    rows = [row for ref, rs in groups.items() for row in (continuity_rows(rs) if ref is not None else
                                                          [x for r in rs for x in continuity_rows([r])])]
    upsert(con, "xcheck_usage", rows)
    return len(rows)


def write_page_checks(con: sqlite3.Connection, tpl: Template | None, page_id: str) -> int:
    con.execute(f"DELETE FROM xcheck_usage WHERE page_id = ? AND check_kind IN ({','.join('?' * len(PAGE_KINDS))})",
                (page_id, *PAGE_KINDS))
    rows = page_checks(con, tpl, page_id)
    upsert(con, "xcheck_usage", rows)
    return len(rows)


def check_usage(con: sqlite3.Connection, site) -> dict:
    """전부 다시 계산한다 (마무리 단계). 돌려주는 값: 종류별·결과별 수."""
    con.execute("DELETE FROM xcheck_usage")
    for r in con.execute("SELECT u.page_id, u.source_form FROM eq_usage_daily u").fetchall():
        write_page_checks(con, site.templates.get(r["source_form"]), r["page_id"])
    recompute_continuity(con)
    return summary(con)


def summary(con: sqlite3.Connection) -> dict:
    out: dict = {}
    for k, res, n in con.execute("SELECT check_kind, result, COUNT(*) FROM xcheck_usage GROUP BY 1, 2 ORDER BY 1, 2"):
        out.setdefault(k, {})[res] = n
    return out


def _row(u, kind: str, item: str, a, b, d, result: str, *, ref: str | None = None, days: int | None = None,
         other: str | None = None, field_a: str | None = None, field_b: str | None = None) -> dict:
    return {"page_id": u["page_id"], "check_kind": kind, "item": item, "work_date": u["work_date"],
            "equipment_ref": ref, "value_a": a, "value_b": b, "diff": d, "days_between": days, "result": result,
            "other_page_id": other, "field_a": field_a, "field_b": field_b}
