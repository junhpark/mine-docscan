"""양식 핸들러: 추출한 셀을 doc_field 와 업무 테이블로 옮기는 방법.

템플릿은 "어디에 무엇이 있는가"(기하)를, 핸들러는 "그것이 업무상 무엇인가"(의미)를 정한다.
템플릿 YAML 의 `handler:` 가 핸들러를 고른다. 새 종류의 업무 기록이 필요할 때만 핸들러를 추가하고,
같은 종류의 새 양식은 템플릿만 추가한다.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from ..config import Settings
from ..correct import Corrector
from ..forms.sitepack import SitePack
from ..forms.template import Template
from ..imaging.cells import CellObs
from ..recognize.base import CellContext, Recognition, Recognizer
from ..store.db import upsert


@dataclass
class PageContext:
    con: sqlite3.Connection
    settings: Settings
    site: SitePack
    template: Template
    document_id: str
    page_id: str
    page_no: int
    source_name: str
    meta: dict                      # date, vehicle_no, operator … (라벨·파일명 규칙에서)
    aligned: np.ndarray
    obs: list[CellObs]
    recognizer: Recognizer
    corrector: Corrector
    notes: dict = field(default_factory=dict)

    @property
    def work_date(self) -> str | None:
        return self.meta.get("date")


def field_id(ctx: PageContext, o: CellObs) -> str:
    return f"{ctx.page_id}:{o.cell.region}:{o.cell.name}:{o.cell.row}"


def field_row(ctx: PageContext, o: CellObs, *, has_value: bool | None, value_raw: str | None,
              value_final: str | None, confidence: float | None, candidates: list[str] | None,
              backend: str, review_status: str) -> dict:
    x0, y0, x1, y1 = o.cell.bbox
    return {
        "field_id": field_id(ctx, o), "page_id": ctx.page_id, "region": o.cell.region, "row_no": o.cell.row,
        "field_name": o.cell.name, "kind": o.cell.kind, "row_key": o.cell.row_key,
        "x0": x0, "y0": y0, "x1": x1, "y1": y1, "ink": o.ink,
        "has_value": None if has_value is None else int(has_value),
        "value_raw": value_raw, "value_final": value_final, "confidence": confidence,
        "candidates": json.dumps(candidates or [], ensure_ascii=False), "backend": backend,
        "review_status": review_status, "reviewed_by": None, "reviewed_at": None,
    }


def recognize(ctx: PageContext, cells: list[CellObs], choices: dict[str, list[str]] | None = None) -> list[Recognition]:
    """셀 묶음을 인식 → 교정까지 돌린다. 값이 없는 셀은 호출 전에 걸러서 넘긴다."""
    if not cells:
        return []
    source = f"{ctx.source_name}#{ctx.page_no}"
    contexts = [CellContext(ctx.template.name, o.cell.region, o.cell.name, o.cell.kind, o.cell.row_key,
                            ctx.work_date, ctx.page_id, source, (choices or {}).get(o.cell.name, []))
                for o in cells]
    recs = ctx.recognizer.recognize([o.crop for o in cells], contexts)
    return ctx.corrector.correct(recs, contexts)


class FormHandler:
    """기본 핸들러: 모든 셀을 doc_field 에만 적재한다 (업무 테이블 없음)."""

    name = "generic"
    text_ink_min = 0.008

    def load(self, ctx: PageContext) -> dict:
        rows, hw = [], []
        for o in ctx.obs:
            if o.cell.kind == "printed":
                rows.append(field_row(ctx, o, has_value=None, value_raw=None,
                                      value_final=_printed_value(o), confidence=1.0, candidates=None,
                                      backend="template", review_status="auto"))
            elif o.cell.kind.startswith("handwritten"):
                hw.append(o)
            else:   # checkmark, signature: 값의 유무만 기록
                rows.append(field_row(ctx, o, has_value=o.ink >= self.text_ink_min, value_raw=None,
                                      value_final=None, confidence=None, candidates=None,
                                      backend="ink", review_status="auto"))
        rows += self.load_handwritten(ctx, hw)
        upsert(ctx.con, "doc_field", rows)
        return {"fields": len(rows), "pending": sum(r["review_status"] == "pending" for r in rows)}

    def load_handwritten(self, ctx: PageContext, hw: list[CellObs]) -> list[dict]:
        filled = [o for o in hw if o.ink >= self.text_ink_min]
        recs = dict(zip([id(o) for o in filled], recognize(ctx, filled), strict=True))
        rows = []
        for o in hw:
            r = recs.get(id(o))
            if r is None:        # 잉크가 없으면 빈 셀로 확정
                rows.append(field_row(ctx, o, has_value=False, value_raw="", value_final="", confidence=1.0,
                                      candidates=None, backend="ink", review_status="auto"))
            else:
                ok = bool(r.text) and r.confidence >= ctx.settings.auto_accept_conf
                rows.append(field_row(ctx, o, has_value=True, value_raw=r.text, value_final=r.text,
                                      confidence=r.confidence, candidates=r.candidates, backend=r.backend,
                                      review_status="auto" if ok else "pending"))
        return rows

    def finalize(self, con: sqlite3.Connection, site: SitePack, settings: Settings) -> dict:
        """모든 문서를 처리한 뒤 한 번 호출된다 (양식 간 교차검증 등)."""
        return {}


def _printed_value(o: CellObs) -> str | None:
    v = o.cell.row_meta.get(o.cell.name)
    return None if v is None else str(v)
