"""운반 실적 핸들러: 숫자 셀(운반 횟수)을 prod_haul 로 옮긴다. 양식은 두 가지 역할 중 하나다.

  role: log     차량별 일보 — 한 장이 차량 한 대. 행 = 광종×편, 열 = 근무조(주간/연근).
                차량번호·작성자는 표 밖에 손으로 적는다 (인식기가 없으면 사이트 팩의 라벨에서 읽는다).
  role: matrix  편×차량 행렬 — 한 장에 여러 차량. 행 = 광종×편, 열 = 차량 자리(slot).
                열 머리글의 운전자·차량번호는 인쇄되어 있지만 실제와 다를 수 있다.

같은 값이 두 양식에 적히므로 finalize() 에서 교차검증한다 (validate/crosscheck.py).

handler_options: role, region(숫자 셀이 있는 표 이름)
행 메타: material, level  /  열 메타: log → shift, matrix → slot, header_operator, header_vehicle_no
"""
from __future__ import annotations

from ..imaging.blobs import assign_blobs
from ..store.db import upsert
from ..validate.crosscheck import crosscheck_haul
from .base import FormHandler, PageContext, field_id, field_row, recognize

MIN_BLOB_AREA = 40      # 셀에 배정된 잉크 면적(px)이 이 이상이면 값이 적힌 것으로 본다


class HaulHandler(FormHandler):
    name = "haul"
    text_ink_min = 0.006

    def load(self, ctx: PageContext) -> dict:
        tpl, opt = ctx.template, ctx.template.handler_options
        role = opt.get("role", "log")
        haul_region = opt.get("region") or tpl.regions[0]["name"]

        # 괘선 제거 + RLSA 로 글씨 덩어리를 셀에 배정. 여러 셀에 걸친 메모는 값으로 치지 않는다
        area: dict[int, int] = {}
        n_notes = 0
        for reg in tpl.regions:
            cells = [o for o in ctx.obs if o.cell.region == reg["name"] and o.cell.kind == "handwritten_number"]
            if not cells:
                continue
            ink_by, blobs = assign_blobs(ctx.aligned, [o.cell for o in cells], reg["grid"]["ys"], reg["grid"]["xs"])
            for ci, a in ink_by.items():
                area[id(cells[ci])] = a
            n_notes += sum(b.is_note for b in blobs)

        numbers = [o for o in ctx.obs if o.cell.kind == "handwritten_number"]
        filled = [o for o in numbers if area.get(id(o), 0) >= MIN_BLOB_AREA]
        recs = dict(zip([id(o) for o in filled], recognize(ctx, filled), strict=True))

        rows, haul = [], []
        for o in ctx.obs:
            c = o.cell
            if c.kind == "handwritten_number":
                r = recs.get(id(o))
                trips, conf, status, raw = None, None, "auto", ""
                if r is not None:
                    raw, conf = r.text, r.confidence
                    trips = int(r.text) if r.text.strip().isdigit() else None
                    ok = trips is not None and r.confidence >= ctx.settings.auto_accept_conf
                    status = "auto" if ok else "pending"
                rows.append(field_row(ctx, o, has_value=r is not None, value_raw=raw,
                                      value_final=None if trips is None else str(trips), confidence=conf,
                                      candidates=r.candidates if r else None,
                                      backend=r.backend if r else "ink", review_status=status))
                if c.region == haul_region:
                    haul.append(self._haul_row(ctx, o, role, r is not None, trips, conf, status))
            elif c.kind == "printed":
                v = c.row_meta.get(c.name)
                rows.append(field_row(ctx, o, has_value=None, value_raw=None, value_final=None if v is None else str(v),
                                      confidence=1.0, candidates=None, backend="template", review_status="auto"))
            else:
                has = o.ink >= self.text_ink_min
                pending = has and c.kind.startswith("handwritten")
                rows.append(field_row(ctx, o, has_value=has, value_raw=None, value_final=None, confidence=None,
                                      candidates=None, backend="ink", review_status="pending" if pending else "auto"))
        upsert(ctx.con, "doc_field", rows)
        upsert(ctx.con, "prod_haul", haul)
        return {"fields": len(rows), "haul_cells": len(haul), "haul_filled": sum(h["has_value"] for h in haul),
                "notes": n_notes}

    def _haul_row(self, ctx: PageContext, o, role: str, has: bool, trips, conf, status) -> dict:
        c = o.cell
        if role == "matrix":
            slot = str(c.col_meta["slot"])
            vehicle, operator = c.col_meta.get("header_vehicle_no"), c.col_meta.get("header_operator")
            shift = c.col_meta.get("shift")
        else:
            slot = None                                     # 교차검증 단계에서 정한다
            vehicle, operator = ctx.meta.get("vehicle_no"), ctx.meta.get("operator")
            shift = c.col_meta.get("shift")
        fid = field_id(ctx, o)
        return {
            "haul_id": fid, "work_date": ctx.work_date, "source_form": ctx.template.name, "source_role": role,
            "page_id": ctx.page_id, "slot": slot,
            "vehicle_no": None if vehicle is None else str(vehicle), "operator": operator,
            "material": str(c.row_meta["material"]), "level": str(c.row_meta["level"]), "shift": shift,
            "has_value": int(has), "trips": trips, "confidence": conf, "source_field_id": fid,
            "review_status": status if has else "auto",
        }

    def finalize(self, con, site, settings) -> dict:
        return {"xcheck_haul": crosscheck_haul(con, exclude_materials=site.option("crosscheck.haul", "exclude_materials", []))}
