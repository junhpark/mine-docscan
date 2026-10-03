"""운반 실적 핸들러: 숫자 셀(운반 횟수)을 prod_haul 로 옮긴다. 양식은 두 가지 역할 중 하나다.

  role: log     차량별 일보 — 한 장이 차량 한 대. 행 = 광종×편, 열 = 근무조(주간/연근).
                차량번호·작성자는 표 밖에 손으로 적는다 (인식기가 없으면 사이트 팩의 라벨에서 읽는다).
  role: matrix  편×차량 행렬 — 한 장에 여러 차량. 행 = 광종×편, 열 = 차량 자리(slot).
                열 머리글의 운전자·차량번호는 인쇄되어 있지만 실제와 다를 수 있다.

같은 값이 두 양식에 적히므로 finalize() 에서 교차검증한다 (validate/crosscheck.py).
prod_haul 행은 검수를 적용한 **최종** 필드 행에서 만든다: trips 는 최종 횟수, trips_raw 는 기계가 읽은 횟수.
검수를 저장하면 on_review() 가 그 행과 그 날짜의 교차검증만 다시 계산한다.

handler_options: role, region(숫자 셀이 있는 표 이름)
행 메타: material, level  /  열 메타: log → shift, matrix → slot, header_operator, header_vehicle_no
"""
from __future__ import annotations

from ..imaging.blobs import assign_blobs
from ..review.store import page_meta
from ..store.db import upsert
from ..validate.crosscheck import crosscheck_haul
from .base import (
    FormHandler,
    PageContext,
    apply_reviews,
    auto_threshold,
    field_id,
    field_row,
    number_status,
    recognize,
    trips_max,
)

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

        rows, haul_cells = [], []
        max_trips = trips_max(ctx.site)
        for o in ctx.obs:
            c = o.cell
            if c.kind == "handwritten_number":
                r = recs.get(id(o))
                trips, conf, status, raw, has = None, None, "auto", "", False
                if r is not None:
                    raw, conf = r.text, r.confidence
                    trips = _as_trips(r.text)
                    if r.answer is not None:                     # 숫자 인식기: 4.4 의 표 (빈 칸 자동 적재, 범위, 거절)
                        # 범위([haul] trips_max)는 운반 횟수 칸만 — 같은 쪽의 다른 숫자 칸(곁표)에는 대지 않는다
                        has, status = number_status(r, ctx.settings, max_trips if c.region == haul_region else None)
                    else:                                        # 예전 규칙 (null·oracle): 숫자로 읽혔고 신뢰도가 높으면
                        has = True
                        ok = trips is not None and r.confidence >= auto_threshold(r, ctx.settings)
                        status = "auto" if ok else "pending"
                rows.append(field_row(ctx, o, has_value=has, value_raw=raw,
                                      value_final=None if trips is None else str(trips), confidence=conf,
                                      candidates=r.candidates if r else None,
                                      backend=r.backend if r else "ink", review_status=status))
                if c.region == haul_region:
                    haul_cells.append((o, rows[-1]["field_id"]))
            elif c.kind == "printed":
                v = c.row_meta.get(c.name)
                rows.append(field_row(ctx, o, has_value=None, value_raw=None, value_final=None if v is None else str(v),
                                      confidence=1.0, candidates=None, backend="template", review_status="auto"))
            else:
                has = o.ink >= self.text_ink_min
                pending = has and c.kind.startswith("handwritten")
                rows.append(field_row(ctx, o, has_value=has, value_raw=None, value_final=None, confidence=None,
                                      candidates=None, backend="ink", review_status="pending" if pending else "auto"))
        rows = apply_reviews(ctx, rows)                      # 검수가 있으면 최종값으로 덮는다
        upsert(ctx.con, "doc_field", rows)
        final = {r["field_id"]: r for r in rows}
        haul = [self._haul_row(ctx, o, role, final[fid]) for o, fid in haul_cells]
        upsert(ctx.con, "prod_haul", haul)
        return {"fields": len(rows), "haul_cells": len(haul), "haul_filled": sum(h["has_value"] for h in haul),
                "notes": n_notes}

    def _haul_row(self, ctx: PageContext, o, role: str, frow: dict) -> dict:
        """최종 필드 행 → prod_haul 행. 값·상태는 haul_values() 가 정한다 (on_review 와 같은 규칙)."""
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
            **haul_values(frow), "source_field_id": fid,
        }

    def finalize(self, con, site, settings) -> dict:
        return {"xcheck_haul": crosscheck_haul(con, exclude_materials=self._exclude(site))}

    def machine_final(self, row: dict) -> str | None:
        t = _as_trips(row["value_raw"])
        return None if t is None else str(t)

    def on_review(self, con, site, settings, field_id: str) -> None:
        """검수 직후. 운반 셀이면 그 prod_haul 행을 최종 필드 행으로 갱신하고, 쪽의 메타 필드(차량번호·작성자)면
        그 쪽의 prod_haul 행 전부의 차량·작성자를 다시 정한다. 어느 쪽이든 그 날짜의 교차검증을 다시 계산한다."""
        frow = con.execute("SELECT f.*, p.template_name, p.page_no, p.work_date, d.source_name FROM doc_field f "
                           "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
                           "WHERE f.field_id = ?", (field_id,)).fetchone()
        if frow is None:
            return
        date = None
        if frow["region"] == "fields":
            tpl = site.templates.get(frow["template_name"])
            if tpl is None or frow["field_name"] not in tpl.meta_fields() or tpl.handler_options.get("role", "log") != "log":
                return
            meta = page_meta(con, site, frow["source_name"], frow["page_no"], frow["page_id"], tpl)
            vehicle, operator = meta.get("vehicle_no"), meta.get("operator")
            con.execute("UPDATE prod_haul SET vehicle_no=?, operator=? WHERE page_id=? AND source_role='log'",
                        (None if vehicle is None else str(vehicle), operator, frow["page_id"]))
            date = frow["work_date"]
        else:
            h = con.execute("SELECT * FROM prod_haul WHERE haul_id = ?", (field_id,)).fetchone()
            if h is None:
                return
            upsert(con, "prod_haul", {**dict(h), **haul_values(dict(frow))})
            date = h["work_date"]
        if date:
            crosscheck_haul(con, exclude_materials=self._exclude(site), dates=[date])

    @staticmethod
    def _exclude(site) -> list[str]:
        return site.option("crosscheck.haul", "exclude_materials", [])


def _as_trips(text: str | None) -> int | None:
    return int(text) if text is not None and text.strip().isdigit() else None


def haul_values(frow: dict) -> dict:
    """최종 필드 행에서 prod_haul 의 값·상태. 행의 구성 필드가 하나뿐이므로 상태는 그 필드의 상태다."""
    has = int(bool(frow["has_value"]))
    return {"has_value_raw": int(bool(frow["has_value_raw"])), "has_value": has,
            "trips": _as_trips(frow["value_final"]), "trips_raw": _as_trips(frow["value_raw"]),
            "confidence": frow["confidence"], "review_status": frow["review_status"]}
