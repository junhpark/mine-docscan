"""점검표 핸들러: 행 = 장비, 열 = (인쇄된 장비 정보, 수기 점검내역, 이상 유/무 체크) → insp_daily.

handler_options:
  region:      표 이름 (기본: 첫 번째 표)
  yes_column:  이상 '유' 체크 칸의 컬럼 이름 (기본 abnormal_yes) — 왼쪽 칸
  no_column:   이상 '무' 체크 칸의 컬럼 이름 (기본 abnormal_no)
  text_column: 점검내역 컬럼 이름 (기본 remark)

행 메타: key(장비 키), category, model, registration. 모델과 등록번호가 모두 빈 행은 양식의 여백 행으로 본다.

insp_daily 행은 검수를 적용한 **최종** 필드 행(유 체크, 무 체크, 점검내역)에서 만든다 (daily_values).
행의 상태: 구성 필드 중 하나라도 pending 이면 pending, 아니고 하나라도 reviewed 면 reviewed, 아니면 auto.
"""
from __future__ import annotations

import uuid

from ..forms.template import row_key as _row_key
from ..imaging.marks import decide_mark_pairs
from ..store.db import upsert
from .base import FormHandler, PageContext, apply_reviews, field_id, field_row

EQ_NAMESPACE = uuid.UUID("5f1c2a2e-7d0b-4a7f-9a3e-2b1e2c3d4e5f")


def equipment_id(equipment_key: str) -> str:
    """같은 장비 키는 언제 어디서 돌려도 같은 UUID 가 된다."""
    return str(uuid.uuid5(EQ_NAMESPACE, equipment_key))


class InspectionHandler(FormHandler):
    name = "inspection"

    def load(self, ctx: PageContext) -> dict:
        tpl, opt = ctx.template, ctx.template.handler_options
        region = opt.get("region") or tpl.regions[0]["name"]
        yes_col, no_col = opt.get("yes_column", "abnormal_yes"), opt.get("no_column", "abnormal_no")
        text_col = opt.get("text_column", "remark")
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
                              "equipment_id": eid, **vals, "entry_source": "scan"})
            upsert(ctx.con, "insp_daily", daily)
        return {"fields": len(rows), "rows_auto": n_auto, "rows_pending": n_pending,
                "marks_undecided": sum(1 for m in marks.values() if m.choice is None),
                "marks_status": _count(m.status for m in marks.values())}

    def _ensure_equipment(self, ctx: PageContext, region: str) -> dict[str, str]:
        eq, rows = {}, []
        for r in ctx.template.region(region)["rows"]:
            key = str(r.get("key", ""))
            model, reg = str(r.get("model", "") or ""), str(r.get("registration", "") or "")
            if not key or not (model or reg.strip("-")):
                continue
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
        opt = tpl.handler_options
        region = opt.get("region") or tpl.regions[0]["name"]
        if f["region"] != region:
            return
        cols = (opt.get("yes_column", "abnormal_yes"), opt.get("no_column", "abnormal_no"), opt.get("text_column", "remark"))
        trow = next((r for r in tpl.region(region)["rows"] if r["row"] == f["row_no"]), None)
        if trow is None or not str(trow.get("key", "")):
            return
        eid = equipment_id(_row_key(trow))
        d = con.execute("SELECT * FROM insp_daily WHERE inspection_id = ?", (f"{f['work_date']}:{eid}",)).fetchone()
        if d is None:
            return
        by = {r["field_name"]: dict(r) for r in con.execute(
            "SELECT * FROM doc_field WHERE page_id = ? AND region = ? AND row_no = ?", (f["page_id"], region, f["row_no"]))}
        upsert(con, "insp_daily", {**dict(d), **daily_values(*(by.get(c) for c in cols))})


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


__all__ = ["InspectionHandler", "equipment_id", "field_id"]
