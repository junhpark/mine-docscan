"""✓ 판정의 정답 (tasks/0004 단계 6): 점검표의 행 하나 = 유·무 두 체크 칸.

행의 답은 새 판정 종류가 아니라 **두 칸의 기존 판정**으로 적는다 (4.9):
  유        = 유 칸 value("1") · 무 칸 empty
  무        = 유 칸 empty · 무 칸 value("1")
  표시 없음 = 둘 다 empty
  모름      = 둘 다 illegible
그래서 검수 기록의 형식, apply_verdict, 핸들러의 daily_values·on_review 를 그대로 쓴다. 두 칸은 한 번의 저장으로 같이 적는다.

기계의 답은 doc_field 의 기계 열(has_value_raw)에서 읽는다 — 검수가 건드리지 않는 값이다:
  유 칸 1 → 유, 무 칸 1 → 무, 둘 다 NULL → 그 쪽의 모든 행이 NULL 이면 "표시 없음"(imaging/marks.py 의 column_unused: 판정 불가가
  80 % 이상이면 쪽 전체를 그렇게 둔다 — 그래서 쪽 전체가 NULL ⇔ column_unused), 아니면 "판정 불가".
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .store import Review, effective

YES, NO, NONE, UNKNOWN, UNDECIDED = "유", "무", "표시 없음", "모름", "판정 불가"
ANSWERS = {"yes": YES, "no": NO, "none": NONE, "unknown": UNKNOWN}
# 답 → (유 칸 판정, 무 칸 판정)
VERDICTS = {YES: ("value", "empty"), NO: ("empty", "value"), NONE: ("empty", "empty"), UNKNOWN: ("illegible", "illegible")}


@dataclass
class CheckRow:
    """점검표 쪽의 장비 행 하나 (유·무 두 칸)."""
    item_id: str                  # check:<page_id>:<region>:<row_no>
    page_id: str
    work_date: str | None
    template_name: str
    source_name: str
    page_no: int
    row_no: int
    row_key: str
    yes: sqlite3.Row
    no: sqlite3.Row
    page_unused: bool             # 기계: 그 쪽의 체크 칸(여백 행 포함)이 전부 NULL — column_unused


def check_rows(con: sqlite3.Connection, site, page_id: str | None = None) -> list[CheckRow]:
    """inspection 핸들러 양식의 쪽마다 장비 행(여백 행 제외 — insp_daily 로 가는 행만). 날짜 → 쪽 → 행 순서."""
    from ..forms.equipment import is_equipment_row, layout

    out: list[CheckRow] = []
    for tpl in site.templates.values():
        if tpl.handler != "inspection":
            continue
        region, yes_col, no_col, _text = layout(tpl)
        rows = {r["row"]: r for r in tpl.region(region)["rows"] if is_equipment_row(r)}
        sql = ("SELECT f.*, p.work_date, p.page_no, d.source_name FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
               "JOIN doc_document d ON p.document_id = d.document_id WHERE p.template_name = ? AND p.status = 'loaded' "
               "AND f.region = ? AND f.kind = 'checkmark' AND f.field_name IN (?, ?)")
        args: list = [tpl.name, region, yes_col, no_col]
        if page_id is not None:
            sql += " AND f.page_id = ?"
            args.append(page_id)
        by: dict[tuple, dict] = {}
        decided: set[str] = set()
        for f in con.execute(sql, args):
            if f["has_value_raw"] is not None:
                decided.add(f["page_id"])
            if f["row_no"] in rows:
                by.setdefault((f["page_id"], f["row_no"]), {})[f["field_name"]] = f
        for (pid, row_no), d in by.items():
            if yes_col in d and no_col in d:
                y = d[yes_col]
                out.append(CheckRow(f"check:{pid}:{region}:{row_no}", pid, y["work_date"], tpl.name, y["source_name"],
                                    y["page_no"], row_no, y["row_key"] or "", y, d[no_col], pid not in decided))
    return sorted(out, key=lambda c: (c.work_date or "", c.source_name, c.page_no, c.row_no))


def row_of(con: sqlite3.Connection, site, field_id: str) -> CheckRow | None:
    """유 칸이나 무 칸의 field_id → 그 행. 점검표의 장비 행 체크 칸이 아니면 None."""
    f = con.execute("SELECT page_id, row_no, region FROM doc_field WHERE field_id = ?", (field_id,)).fetchone()
    if f is None:
        return None
    return next((c for c in check_rows(con, site, f["page_id"])
                 if c.row_no == f["row_no"] and field_id in (c.yes["field_id"], c.no["field_id"])), None)


def truth_of(yes: Review | None, no: Review | None) -> str | None:
    """두 칸의 유효한 검수 → 행의 답. 둘 중 하나라도 없거나 4.9 의 네 가지가 아니면 None (정답 없음)."""
    if yes is None or no is None:
        return None
    pair = (yes.verdict, no.verdict)
    return next((a for a, v in VERDICTS.items() if v == pair), None)


def answers(con: sqlite3.Connection, rows: list[CheckRow]) -> dict[str, str]:
    """item_id → 검수로 정해진 답 (정답이 있는 행만)."""
    eff = effective(con, field_ids=[f for c in rows for f in (c.yes["field_id"], c.no["field_id"])])
    out = {}
    for c in rows:
        a = truth_of(eff.get(c.yes["field_id"]), eff.get(c.no["field_id"]))
        if a is not None:
            out[c.item_id] = a
    return out


def machine_answers(rows: list[CheckRow]) -> dict[str, str]:
    """item_id → 기계의 답 (유 / 무 / 표시 없음 / 판정 불가). 기계 열만 본다."""
    out = {}
    for c in rows:
        if c.yes["has_value_raw"] == 1:
            out[c.item_id] = YES
        elif c.no["has_value_raw"] == 1:
            out[c.item_id] = NO
        else:
            out[c.item_id] = NONE if c.page_unused else UNDECIDED
    return out


def check_reviews(con: sqlite3.Connection, row: CheckRow, answer: str, reviewer: str, note: str = "") -> list[Review]:
    """행의 답 → 두 칸의 검수 두 건 (같은 시각). answer 는 유 / 무 / 표시 없음 / 모름."""
    from .store import now_iso, review_from_field

    if answer not in VERDICTS:
        raise ValueError(f"답은 {tuple(VERDICTS)} 중 하나: {answer}")
    at = now_iso()
    out = []
    for f, verdict in zip((row.yes, row.no), VERDICTS[answer], strict=True):
        out.append(review_from_field(con, f["field_id"], verdict, "1" if verdict == "value" else "", reviewer,
                                     note=note, reviewed_at=at))
    return out
