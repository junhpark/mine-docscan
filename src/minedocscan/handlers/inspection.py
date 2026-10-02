"""점검표 핸들러: 행 = 장비, 열 = (인쇄된 장비 정보, 수기 점검내역, 이상 유/무 체크) → insp_daily.

handler_options:
  region:      표 이름 (기본: 첫 번째 표)
  yes_column:  이상 '유' 체크 칸의 컬럼 이름 (기본 abnormal_yes) — 왼쪽 칸
  no_column:   이상 '무' 체크 칸의 컬럼 이름 (기본 abnormal_no)
  text_column: 점검내역 컬럼 이름 (기본 remark)

행 메타: key(장비 키), category, model, registration. 모델과 등록번호가 모두 빈 행은 양식의 여백 행으로 본다.
"""
from __future__ import annotations

import uuid

from ..imaging.marks import decide_mark_pairs
from ..store.db import upsert
from .base import FormHandler, PageContext, field_id, field_row

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
        hw_rows = self.load_handwritten(ctx, hw)
        rows += hw_rows
        upsert(ctx.con, "doc_field", rows)

        # ── 검증 + 업무 테이블 ──
        remark = {r["row_no"]: r for r in hw_rows if r["region"] == region and r["field_name"] == text_col}
        n_auto = n_pending = 0
        if ctx.work_date:
            daily = []
            for r in tpl.region(region)["rows"]:
                eid = eq.get(str(r.get("key", "")))
                m = marks.get(r["row"])
                if not eid or m is None:
                    continue
                rem = remark.get(r["row"])
                abnormal = None if m.choice is None else (m.choice == "first")
                status = "auto"
                if m.status != "ok" or rem is None or rem["review_status"] != "auto":
                    status = "pending"
                if abnormal is True and (rem is None or not rem["has_value"]):
                    status = "pending"          # 이상 '유' 인데 점검내역이 비어 있다
                n_auto += status == "auto"
                n_pending += status == "pending"
                daily.append({
                    "inspection_id": f"{ctx.work_date}:{eid}", "inspection_date": ctx.work_date, "equipment_id": eid,
                    "abnormal": None if abnormal is None else int(abnormal),
                    "remark": rem["value_final"] if rem else None, "entry_source": "scan",
                    "source_field_id": rem["field_id"] if rem else None, "review_status": status,
                })
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


def _count(items) -> dict:
    out: dict = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


__all__ = ["InspectionHandler", "equipment_id", "field_id"]
