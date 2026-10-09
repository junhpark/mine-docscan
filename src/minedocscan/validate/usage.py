"""가동 일보의 검산 (tasks/0005 4.4) → xcheck_usage. 보여 주는 것이다 — 값을 고치지 않는다, 앞날의 종료로 오늘의 시작을 채우지 않는다
(ADR 0006). 어긋난 것은 검수 대기열(usage-check)에서 사람이 두 칸을 같이 본다.

  쪽 안   total       총 = 종료 − 시작 (셋 다 계기 값일 때)                        match | mismatch
          subtotal    소계 = 합 (작업량 표의 소계 칸 — 템플릿의 subtotal: true)    match | mismatch | unknown(모르는 칸이 있다)
  날짜 사이 continuity 같은 장비의, 계기 값이 있는 바로 앞 기록의 종료 = 이 기록의 시작
                      match | gap(시작이 더 크다 — 빠진 날이 있거나 잘못 적었다) | overlap(시작이 더 작다) | first(앞 기록 없음)
                      | unknown(장비를 모른다, 값이 검수 대기다). 차이(시간)와 사이에 낀 날 수를 같이 남긴다

같은 장비 = 장비 ID 가 있으면 ID, 없으면 적힌 이름이 같은 것 (equipment_ref: "id:<UUID>" | "name:<이름>"). 이름이 없는 쪽은 unknown.
기록의 순서: 날짜 → 계기 시작 값이 작은 쪽 → 문서 이름 → 쪽 번호 (하루 두 장 — tasks/0005 9절). 바로 앞 기록의 종료를 모르면
(검수 대기, 시작만 적힌 기록), 같은 날 시작 값을 모르는 다른 장이 있으면, 날짜를 모르면 비교하지 않고 unknown — 일부만 검수한 중에
가짜 gap 이 생기지 않게 (crosscheck 와 같은 생각).

마무리(finalize)에서 전체를, 검수를 저장하면 그 쪽의 쪽 안 검산과 그 장비(이름이 바뀌었으면 예전·새 장비 둘 다)의 연속성만
다시 계산한다. 연속성은 장비마다 그 장비의 기록만으로 정해지는 함수라 둘의 결과가 같다 (불변식).
"""
from __future__ import annotations

import sqlite3
from datetime import date

from ..forms.formats import default_format
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
        "SELECT region, row_no, field_name, field_id, has_value, value_final, review_status FROM doc_field WHERE page_id = ? "
        "AND format = 'integer'", (u["page_id"],))}
    out = []
    for reg in tpl.regions:
        if reg.get("role") != "tally":
            continue
        name = reg["name"]
        for (row, col), parts in subtotal_pairs(reg):
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


def subtotal_pairs(reg: dict) -> list[tuple[tuple[int, str], list[tuple[int, str]]]]:
    """작업량 표(role tally)의 소계 칸마다 (소계 칸, 더할 칸들) — 칸은 (행 번호, 열 이름). 소계 열은 같은 행의 소계가 아닌 열,
    소계 행은 같은 열의 소계가 아닌 행. 둘 다 소계인 칸(합계의 합계)은 소계 행의 소계가 아닌 열."""
    cols = [c for c in reg["columns"] if c.get("kind", "").startswith("handwritten")
            and (c.get("format") or default_format(c.get("kind", ""))) == "integer"]      # 글자 칸(비고 …)은 더하지 않는다
    sub_cols = [c["name"] for c in cols if c.get("subtotal")]
    val_cols = [c["name"] for c in cols if not c.get("subtotal")]
    sub_rows = [r["row"] for r in reg["rows"] if r.get("subtotal")]
    val_rows = [r["row"] for r in reg["rows"] if not r.get("subtotal")]
    pairs = [((row, sc), [(row, vc) for vc in val_cols]) for row in [*val_rows, *sub_rows] for sc in sub_cols]
    pairs += [((sr, vc), [(vr, vc) for vr in val_rows]) for sr in sub_rows for vc in val_cols]
    return pairs


def check_cells(con: sqlite3.Connection, site, check) -> list[str]:
    """검산 하나가 비교한 칸들의 field_id (검수 화면이 같이 보여 준다): 연속성 = 이 쪽의 시작·앞 기록의 종료,
    총 = 시작·종료·총, 소계 = 소계 칸과 더한 칸들."""
    if check["check_kind"] == "continuity":
        return [f for f in (check["field_a"], check["field_b"]) if f]
    if check["check_kind"] == "total":
        u = con.execute("SELECT start_field_id, end_field_id, total_field_id FROM eq_usage_daily WHERE page_id = ?",
                        (check["page_id"],)).fetchone()
        return [f for f in (u or ()) if f]
    f = con.execute("SELECT f.region, f.row_no, f.field_name, p.template_name FROM doc_field f JOIN doc_page p "
                    "ON f.page_id = p.page_id WHERE f.field_id = ?", (check["field_a"],)).fetchone()
    tpl = site.templates.get(f["template_name"]) if f is not None else None
    if tpl is None:
        return [check["field_a"]]
    for (row, col), parts in subtotal_pairs(tpl.region(f["region"])):
        if (row, col) == (f["row_no"], f["field_name"]):
            return [check["field_a"]] + [f"{check['page_id']}:{f['region']}:{c}:{r}" for r, c in parts]
    return [check["field_a"]]


def _int(cell) -> int | None:
    """칸의 최종 정수: 빈 칸 0, 모르는 칸(잉크는 있는데 값이 없다, 검수 대기·읽을 수 없음) None."""
    v = cell["value_final"]
    if cell["review_status"] == "pending" and v in (None, ""):      # "읽을 수 없음"은 기계의 값 유무를 그대로 둔다 — 빈 칸이 아니다
        return None
    if not cell["has_value"]:
        return 0
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
    """같은 장비의 기록 순서: 날짜, 시작 값(없는 것은 뒤), 문서 이름, 쪽 번호, 쪽 ID — 끝까지 같으면 쪽 ID 가 가른다
    (스캐너는 같은 파일명을 다시 쓴다 — tasks/0007 4.8. 정렬이 행이 들어간 순서에 기대지 않게)."""
    s = r["meter_start"]
    return (r["work_date"] or "", s is None, s if s is not None else 0.0, r["source_name"], r["page_no"], r["page_id"])


def continuity_rows(records: list[dict]) -> list[dict]:
    """한 장비(같은 ref)의 기록들 → 연속성 검산 행. 비교하지 못하면 unknown (가짜 gap 을 만들지 않는다):
      · 장비를 모른다(ref 없음), 날짜를 모른다, 이 기록의 시작 값이 검수 대기다
      · 같은 날 시작 값을 아직 모르는 다른 장이 있다 (그 장이 앞일 수 있다 — 하루 두 장의 순서는 시작 값으로 정한다)
      · 바로 앞의 계기 기록의 종료를 모른다 (검수 대기, 또는 시작만 적혀 있다) — 그보다 앞을 찾지 않는다
    계기 값이 없는 기록(시각·빈 칸)은 건너뛴다."""
    dated = [r for r in records if r["work_date"]]
    out = []
    for r in records:
        if r["meter_start"] is None and r["reading_kind"] != "pending":
            continue                                         # 계기 값이 없는 기록 (시각·빈 칸): 연속성 검산이 없다
        if r["ref"] is None or r["meter_start"] is None or not r["work_date"]:
            out.append(_row(r, "continuity", "", r["meter_start"], None, None, "unknown", ref=r["ref"],
                            field_a=r["start_field_id"]))
            continue
        prev, blocked = None, any(q is not r and q["work_date"] == r["work_date"] and q["meter_start"] is None
                                  and q["reading_kind"] == "pending" for q in dated)
        i = next(k for k, q in enumerate(dated) if q is r)
        for q in reversed(dated[:i]):
            if blocked:
                break
            if q["meter_end"] is not None:
                prev = q
                break
            if q["reading_kind"] == "pending" or q["meter_start"] is not None:   # 앞 기록의 종료를 모른다 — 더 앞을 보지 않는다
                blocked = True
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


def recompute_continuity(con: sqlite3.Connection, refs: set[str | None] | None = None, pages: set[str] = frozenset()) -> set[str]:
    """연속성 검산을 다시 계산한다. refs=None 이면 전부. 아니면 그 장비들(과 pages 의 쪽 — 장비를 모르게 된 쪽 포함)만.
    돌려주는 값: 다시 계산하기 전과 뒤로 견주어 **바뀐 행의 쪽** — 행이 걸린 쪽(page_id)과 가리키는 쪽(other_page_id), 생긴 행·없어진 행·
    값이 바뀐 행 모두 (tasks/0009 4.2 다: 엑셀·싣기의 더러운 범위. 장비의 모든 날짜로 넓히면 칸 하나가 84일을 더럽혔다 — 연속성은
    같은 장비의 이웃한 기록만 잇는다). 장비 이름을 고쳐 장비가 바뀐 경우도 옛 장비·새 장비의 사슬을 둘 다 견주므로 둘 다 잡는다."""
    def snapshot() -> dict[tuple, dict]:
        if refs is None:
            q, args = "SELECT * FROM xcheck_usage WHERE check_kind = 'continuity'", ()
        else:
            conds, args = [], []
            for col, vals in (("equipment_ref", sorted(want)), ("page_id", sorted(pages))):
                if vals:
                    conds.append(f"{col} IN ({','.join('?' * len(vals))})")
                    args += vals
            if not conds:
                return {}
            q = f"SELECT * FROM xcheck_usage WHERE check_kind = 'continuity' AND ({' OR '.join(conds)})"
        return {(r["page_id"], r["check_kind"], r["item"]): dict(r) for r in con.execute(q, args)}

    want = {r for r in refs if r is not None} if refs is not None else set()
    before = snapshot()
    if refs is None:
        con.execute("DELETE FROM xcheck_usage WHERE check_kind = 'continuity'")
        records = _records(con)
    else:
        for col, vals in (("equipment_ref", sorted(want)), ("page_id", sorted(pages))):   # 빈 IN () 은 PostgreSQL 이 받지 않는다
            if vals:
                con.execute(f"DELETE FROM xcheck_usage WHERE check_kind = 'continuity' AND {col} IN "
                            f"({','.join('?' * len(vals))})", vals)
        records = [r for r in _records(con) if r["ref"] in want or (r["ref"] is None and r["page_id"] in pages)]
    groups: dict = {}
    for r in records:
        groups.setdefault(r["ref"], []).append(r)
    rows = [row for ref, rs in groups.items() for row in (continuity_rows(rs) if ref is not None else
                                                          [x for r in rs for x in continuity_rows([r])])]
    upsert(con, "xcheck_usage", rows)
    after = snapshot()
    changed: set[str] = set()
    for key in set(before) | set(after):
        a, b = before.get(key), after.get(key)
        if a != b:
            for row in (a, b):
                if row is not None:
                    changed |= {p for p in (row["page_id"], row["other_page_id"]) if p}
    return changed


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
