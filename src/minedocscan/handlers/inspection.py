"""점검표 핸들러: 행 = 장비, 열 = (인쇄된 장비 정보, 수기 점검내역, 이상 유/무 체크) → insp_daily.

handler_options:
  region:      표 이름 (기본: 첫 번째 표)
  yes_column:  이상 '유' 체크 칸의 컬럼 이름 (기본 abnormal_yes) — 왼쪽 칸
  no_column:   이상 '무' 체크 칸의 컬럼 이름 (기본 abnormal_no)
  text_column: 점검내역 컬럼 이름 (기본 remark)

행 메타: key(장비 키), category, model, registration. 모델과 등록번호가 모두 빈 행은 양식의 여백 행으로 본다.

insp_daily 행은 검수를 적용한 **최종** 필드 행(유 체크, 무 체크, 점검내역)에서 만든다 (daily_values).
행의 상태: 구성 필드 중 하나라도 pending 이면 pending, 아니고 하나라도 reviewed 면 reviewed, 아니면 auto.

같은 날의 두 쪽이 같은 장비를 적으면 **쪽의 순서(store/order.py)가 뒤인 쪽이 이긴다** — 지금의 `run` 이 넣는 순서와 같다
(tasks/0007 4.8). 행에 그 쪽(page_id)을 남기고, 쪽을 지우거나 더한 뒤에는 그 날짜의 점검 행을 남은 쪽들의 최종 필드 행에서 다시
만든다 (finalize). 검수는 이기는 쪽(행의 page_id)의 칸을 검수했을 때만 행을 고친다.
"""
from __future__ import annotations

from ..forms.equipment import equipment_id, is_equipment_row, layout
from ..forms.template import row_key as _row_key
from ..imaging.marks import decide_mark_pairs
from ..store.db import upsert
from ..store.order import page_key
from .base import FormHandler, PageContext, apply_reviews, field_id, field_row

NO_MARKS = "점검 표시 없음 — 체크 열 전체가 비었다 (점검하지 않은 날)"     # 내보내기의 쪽 머리 (tasks/0008 4.2)


class InspectionHandler(FormHandler):
    name = "inspection"

    def export_cells(self, template, fields: dict[str, dict]) -> tuple[dict[str, str], list[str]]:
        """엑셀의 ✓ 칸 (tasks/0008 4.2): 점검하지 않은 쪽(체크 칸 전부의 기계 판정이 NULL — review/checks.py 의 column_unused 와 같은
        유도)은 ✓ 칸을 비우고 쪽의 머리에 한 줄. 여백 행(장비 행이 아닌 행)의 ✓ 칸은 기계가 표시를 보지 못했으면 비운다 — 날마다 검수
        대기이고 검수할 길이 없는 칸이다. 사람이 본 칸(검수가 있는 칸)은 고치지 않는다."""
        region, yes_col, no_col, _text = layout(template)
        marks = [f for f in fields.values()
                 if f["region"] == region and f["kind"] == "checkmark" and f["field_name"] in (yes_col, no_col)]
        if not marks:
            return {}, []

        def machine_pending(f: dict) -> bool:
            return f["review_status"] == "pending" and f["reviewed_by"] is None

        if all(f["has_value_raw"] is None for f in marks):
            seen = any(f["reviewed_by"] and f["has_value"] for f in marks)     # 사람이 표시를 확인한 칸이 있다 — 그 줄은 틀린 말이 된다
            return {f["field_id"]: "empty" for f in marks if machine_pending(f)}, ([] if seen else [NO_MARKS])
        eq_rows = {r["row"] for r in template.region(region)["rows"] if is_equipment_row(r)}
        return {f["field_id"]: "empty" for f in marks
                if f["row_no"] not in eq_rows and machine_pending(f) and f["has_value_raw"] != 1}, []

    def load(self, ctx: PageContext) -> dict:
        tpl = ctx.template
        region, yes_col, no_col, text_col = layout(tpl)
        eq = self._ensure_equipment(ctx, region)
        marks = decide_mark_pairs(ctx.aligned, [o for o in ctx.obs if o.cell.region == region], yes_col, no_col)

        rows, hw = [], []
        for o in ctx.obs:
            if o.cell.kind == "checkmark" and o.cell.region == region and o.cell.row in marks:
                m = marks[o.cell.row]
                ticked = None if m.choice is None else (m.choice == ("first" if o.cell.name == yes_col else "second"))
                val = None if ticked is None else str(int(ticked))
                rows.append(field_row(ctx, o, has_value=ticked, value_raw=val, value_final=val,
                                      confidence=1.0 if m.status == "ok" else 0.0, candidates=None, backend="ink",
                                      review_status="auto" if m.status == "ok" else "pending"))
            elif o.cell.kind == "printed":
                v = o.cell.row_meta.get(o.cell.name)
                rows.append(field_row(ctx, o, has_value=None, value_raw=None, value_final=None if v is None else str(v),
                                      confidence=1.0, candidates=None, backend="template", review_status="auto"))
            elif o.cell.kind.startswith("handwritten"):
                hw.append(o)
            else:
                rows.append(field_row(ctx, o, has_value=o.ink >= self.text_ink_min, value_raw=None, value_final=None,
                                      confidence=None, candidates=None, backend="ink", review_status="auto"))
        rows += self.load_handwritten(ctx, hw)
        rows = apply_reviews(ctx, rows)                      # 검수가 있으면 최종값으로 덮는다
        upsert(ctx.con, "doc_field", rows)

        # ── 검증 + 업무 테이블: 최종 필드 행에서 ──
        by = {(r["region"], r["row_no"], r["field_name"]): r for r in rows}
        n_auto = n_pending = 0
        if ctx.work_date:
            daily = []
            for r in tpl.region(region)["rows"]:
                eid = eq.get(str(r.get("key", "")))
                if not eid or r["row"] not in marks:
                    continue
                vals = daily_values(by.get((region, r["row"], yes_col)), by.get((region, r["row"], no_col)),
                                    by.get((region, r["row"], text_col)))
                n_auto += vals["review_status"] == "auto"
                n_pending += vals["review_status"] == "pending"
                daily.append({"inspection_id": f"{ctx.work_date}:{eid}", "inspection_date": ctx.work_date,
                              "equipment_id": eid, **vals, "entry_source": "scan", "page_id": ctx.page_id})
            # 쪽마다 커밋하므로 순서가 앞인 쪽을 적재할 때 뒤인 쪽(다른 문서)의 행을 덮어쓰면 그 사이에 끊겼을 때 틀린 행이 남는다
            later = _later_pages(ctx.con, ctx.work_date, ctx.page_id)
            upsert(ctx.con, "insp_daily", [d for d in daily if d["inspection_id"] not in later])
        return {"fields": len(rows), "rows_auto": n_auto, "rows_pending": n_pending,
                "marks_undecided": sum(1 for m in marks.values() if m.choice is None),
                "marks_status": _count(m.status for m in marks.values())}

    def _ensure_equipment(self, ctx: PageContext, region: str) -> dict[str, str]:
        eq, rows = {}, []
        for r in ctx.template.region(region)["rows"]:
            if not is_equipment_row(r):
                continue
            key = str(r.get("key", ""))
            model, reg = str(r.get("model", "") or ""), str(r.get("registration", "") or "")
            eid = equipment_id(key)
            cat = str(r.get("category", "") or "")
            eq[key] = eid
            rows.append({"equipment_id": eid, "equipment_key": key,
                         "hid": " ".join(x for x in (cat, model, reg) if x and x != "-"),
                         "site_category": cat, "iso_type": ctx.site.iso_type(cat), "oem": r.get("oem"),
                         "model": model, "registration": reg, "active": 1})
        upsert(ctx.con, "eq_equipment", rows)
        return eq

    def on_review(self, con, site, settings, field_id: str) -> None:
        """검수 직후: 그 행(장비)의 insp_daily 를 최종 필드 행 세 개로 다시 만든다."""
        f = con.execute("SELECT f.*, p.template_name, p.work_date FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                        "WHERE f.field_id = ?", (field_id,)).fetchone()
        if f is None or not f["work_date"] or f["template_name"] not in site.templates:
            return
        tpl = site.templates[f["template_name"]]
        region, *cols = layout(tpl)
        if f["region"] != region:
            return
        trow = next((r for r in tpl.region(region)["rows"] if r["row"] == f["row_no"]), None)
        if trow is None or not str(trow.get("key", "")):
            return
        eid = equipment_id(_row_key(trow))
        d = con.execute("SELECT * FROM insp_daily WHERE inspection_id = ?", (f"{f['work_date']}:{eid}",)).fetchone()
        if d is None or (d["page_id"] is not None and d["page_id"] != f["page_id"]):
            return                                           # 그 날짜:장비의 행은 다른 쪽(순서가 뒤인 쪽)의 것이다
        by = {r["field_name"]: dict(r) for r in con.execute(
            "SELECT * FROM doc_field WHERE page_id = ? AND region = ? AND row_no = ?", (f["page_id"], region, f["row_no"]))}
        upsert(con, "insp_daily", {**dict(d), **daily_values(*(by.get(c) for c in cols))})


    def finalize(self, con, site, settings, dates=None, equipment=None) -> dict:
        """그 날짜들의 점검 행을 남은 쪽들의 최종 필드 행에서 다시 만든다 (None 이면 점검 행·점검표 쪽이 있는 날짜 전부).
        쪽의 순서가 뒤인 쪽이 이긴다 — 문서를 어느 순서로 다시 처리했든 같다 (tasks/0007 4.8)."""
        if dates is None:
            dates = {r[0] for r in con.execute("SELECT DISTINCT inspection_date FROM insp_daily WHERE entry_source = 'scan'")}
            dates |= {r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE status = 'loaded' AND "
                                                "work_date IS NOT NULL")}
        for d in sorted(x for x in dates if x):
            rebuild_daily(con, site, d)
        return {}


def _page_order(con, page_id: str) -> tuple | None:
    r = con.execute("SELECT p.page_no, d.document_id, d.source_rel, d.source_path FROM doc_page p JOIN doc_document d "
                    "ON p.document_id = d.document_id WHERE p.page_id = ?", (page_id,)).fetchone()
    return None if r is None else page_key(r["source_rel"], r["source_path"], r["document_id"], r["page_no"])


def _later_pages(con, work_date: str, page_id: str) -> set[str]:
    """그 날짜의 점검 행 중 이 쪽보다 쪽의 순서가 뒤인 쪽이 만든 것 (inspection_id) — 이 쪽이 덮어쓰지 않는다."""
    mine = _page_order(con, page_id)
    if mine is None:
        return set()
    out = set()
    for r in con.execute("SELECT inspection_id, page_id FROM insp_daily WHERE inspection_date = ? AND entry_source = 'scan' "
                         "AND page_id IS NOT NULL AND page_id <> ?", (work_date, page_id)):
        other = _page_order(con, r["page_id"])
        if other is not None and other > mine:
            out.add(r["inspection_id"])
    return out


def rebuild_daily(con, site, work_date: str) -> int:
    """그 날짜의 점검 행(entry_source scan)을 그날 적재된 점검표 쪽들의 최종 필드 행에서 다시 만든다 — 쪽의 순서대로 덮어써
    순서가 뒤인 쪽이 이긴다. load 와 같은 규칙: 장비 행이고 유·무 두 칸이 다 있는 행만 (imaging/marks.decide_mark_pairs 가 그런 행을
    다 판정한다). 돌려주는 값: 쓴 행 수."""
    con.execute("DELETE FROM insp_daily WHERE inspection_date = ? AND entry_source = 'scan'", (work_date,))
    pages = con.execute(
        "SELECT p.page_id, p.page_no, p.template_name, d.document_id, d.source_rel, d.source_path FROM doc_page p "
        "JOIN doc_document d ON p.document_id = d.document_id WHERE p.work_date = ? AND p.status = 'loaded'",
        (work_date,)).fetchall()
    n = 0
    for pg in sorted(pages, key=lambda r: page_key(r["source_rel"], r["source_path"], r["document_id"], r["page_no"])):
        tpl = site.templates.get(pg["template_name"])
        if tpl is None or tpl.handler != InspectionHandler.name:
            continue
        region, yes_col, no_col, text_col = layout(tpl)
        by = {(r["row_no"], r["field_name"]): dict(r) for r in con.execute(
            "SELECT * FROM doc_field WHERE page_id = ? AND region = ?", (pg["page_id"], region))}
        daily = []
        for r in tpl.region(region)["rows"]:
            if not is_equipment_row(r) or (r["row"], yes_col) not in by or (r["row"], no_col) not in by:
                continue
            eid = equipment_id(str(r.get("key", "")))
            daily.append({"inspection_id": f"{work_date}:{eid}", "inspection_date": work_date, "equipment_id": eid,
                          **daily_values(by.get((r["row"], yes_col)), by.get((r["row"], no_col)), by.get((r["row"], text_col))),
                          "entry_source": "scan", "page_id": pg["page_id"]})
        n += upsert(con, "insp_daily", daily)
    return n


def daily_values(yes: dict | None, no: dict | None, rem: dict | None) -> dict:
    """최종 필드 행(유 체크, 무 체크, 점검내역) → insp_daily 의 값·상태."""
    if yes is not None and yes["has_value"] == 1:
        abnormal = True
    elif no is not None and no["has_value"] == 1:
        abnormal = False
    else:
        abnormal = None                                      # 판정 불가 (점검을 하지 않은 날 등)
    statuses = [r["review_status"] for r in (yes, no, rem) if r is not None]
    status = "pending" if "pending" in statuses else ("reviewed" if "reviewed" in statuses else "auto")
    if rem is None:
        status = "pending"                                   # 점검내역 칸이 없는 양식은 사람이 봐야 한다
    if abnormal is True and (rem is None or not rem["has_value"]):
        status = "pending"                                   # 이상 '유' 인데 점검내역이 비어 있다
    return {"abnormal": None if abnormal is None else int(abnormal), "remark": rem["value_final"] if rem else None,
            "source_field_id": rem["field_id"] if rem else None, "review_status": status}


def _count(items) -> dict:
    out: dict = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


__all__ = ["InspectionHandler", "equipment_id", "field_id", "is_equipment_row", "layout"]
