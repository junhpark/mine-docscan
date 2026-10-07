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
from ..imaging.cropspec import PageImages, crop_cell
from ..recognize.base import CellContext, Recognition, Recognizer, spec_for
from ..review.store import apply_verdict, effective
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
    images: PageImages | None = None    # 이 쪽의 그림(정합 이미지·원본). 인식기에 넘길 크롭을 규격대로 뜨는 데 쓴다
    print_mask: np.ndarray | None = None  # 인쇄 마스크 (Template.print_mask, 인쇄 층이 없으면 None) — 값 유무에만 쓴다 (tasks/0006 4.3)

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
        "field_name": o.cell.name, "kind": o.cell.kind, "format": o.cell.fmt, "row_key": o.cell.row_key,
        "x0": x0, "y0": y0, "x1": x1, "y1": y1, "ink": o.ink,
        "has_value_raw": None if has_value is None else int(has_value),
        "has_value": None if has_value is None else int(has_value),
        "value_raw": value_raw, "value_final": value_final, "confidence": confidence,
        "candidates": json.dumps(candidates or [], ensure_ascii=False), "backend": backend,
        "review_status": review_status, "status_raw": review_status, "reviewed_by": None, "reviewed_at": None,
    }


def apply_reviews(ctx: PageContext, rows: list[dict]) -> list[dict]:
    """기계가 만든 필드 행에 유효한 검수를 덮는다 (value_final·has_value·review_status·reviewed_by·reviewed_at).

    기계 값(value_raw, confidence, backend, has_value_raw)은 그대로다. 검수된 셀도 인식기는 돌렸으므로
    새 인식기의 value_raw 를 검수값과 비교할 수 있다. 업무 테이블은 이 함수가 돌려준 최종 행에서 만든다.
    """
    reviews = effective(ctx.con, page_id=ctx.page_id)
    if not reviews:
        return rows
    return [apply_verdict(r, reviews[r["field_id"]]) if r["field_id"] in reviews else r for r in rows]


def recognize(ctx: PageContext, cells: list[CellObs], choices: dict[str, list[str]] | None = None) -> list[Recognition]:
    """셀 묶음을 인식 → 교정까지 돌린다. 값이 없는 셀은 호출 전에 걸러서 넘긴다.

    크롭은 핸들러가 만들지 않는다: 인식기가 그 칸 종류에 선언한 규격(spec_for)대로 imaging/cropspec.crop_cell 로 뜬다
    — review export-crops 가 쓰는 것과 같은 구현이라 같은 셀이면 화소까지 같다 (tasks/0003 4.1)."""
    if not cells:
        return []
    source = f"{ctx.source_name}#{ctx.page_no}"
    contexts = [CellContext(ctx.template.name, o.cell.region, o.cell.name, o.cell.kind, o.cell.row_key,
                            ctx.work_date, ctx.page_id, source, (choices or {}).get(o.cell.name, []),
                            field_id=field_id(ctx, o))
                for o in cells]
    images = ctx.images or PageImages(aligned=ctx.aligned)
    crops = [crop_cell(images, o.cell.bbox, spec_for(ctx.recognizer, o.cell.kind)) for o in cells]
    recs = ctx.recognizer.recognize(crops, contexts)
    return ctx.corrector.correct(recs, contexts)


def auto_threshold(r: Recognition, settings: Settings) -> float:
    """자동 적재 기준: 백엔드가 정한 것(모델 카드·[recognize.<백엔드>]) 아니면 [pipeline] auto_accept_conf."""
    return settings.auto_accept_conf if r.threshold is None else r.threshold


def trips_max(site: SitePack) -> int | None:
    """숫자 칸 값의 범위 — 현장의 것: site.toml 의 [haul] trips_max. 없으면 검사하지 않는다 (tasks/0003 4.4)."""
    v = site.option("haul", "trips_max")
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"site.toml 의 [haul] trips_max 는 0 이상의 정수: {v!r}")
    return v


def number_status(r: Recognition, settings: Settings, max_value: int | None = None) -> tuple[bool, str]:
    """숫자 칸의 인식 결과 → (기계의 값 유무, review_status). tasks/0003 4.4 의 표 — 답의 종류를 말하는 백엔드(r.answer)용:

      숫자열, 범위 안   신뢰도 ≥ t → 그 값으로 자동 적재        그 밖 → 값 있음 + 검수 대기
      숫자열, 범위 밖   검수 대기
      빈 칸             신뢰도 ≥ t → 값 없음으로 자동 적재      그 밖 → 값 있음 + 검수 대기
      거절("?")         검수 대기
    인식기는 값을 만들어 내지 않는다: 잉크 판정이 "값 없음"인 칸은 여기 오지 않는다. 읽은 문자열은 value_raw 에 그대로 남긴다.
    """
    sure = r.confidence >= auto_threshold(r, settings)
    if r.answer == "empty":
        return (False, "auto") if sure else (True, "pending")
    if r.answer == "value" and r.text.isdigit() and (max_value is None or int(r.text) <= max_value) and sure:
        return True, "auto"
    return True, "pending"


def as_int(text: str | None) -> int | None:
    """숫자 칸의 값 → 정수. 숫자열이 아니면 None."""
    return int(text) if text is not None and text.strip().isdigit() else None


def number_row(ctx: PageContext, o: CellObs, r: Recognition | None, max_value: int | None = None) -> dict:
    """덩어리 배정으로 값 유무를 정하는 정수 칸(운반 횟수, 작업량)의 doc_field 행. r 이 None 이면 잉크가 없는 칸 — 빈 칸으로 확정.
    value_final 은 읽은 숫자열을 정수로 (앞의 0 을 뗀다), 숫자가 아니면 None. max_value: 범위 검사 (운반 횟수 칸만 — trips_max)."""
    val, conf, status, raw, has = None, None, "auto", "", False
    if r is not None:
        raw, conf = r.text, r.confidence
        val = as_int(r.text)
        if r.answer is not None:                     # 숫자 인식기: tasks/0003 4.4 의 표 (빈 칸 자동 적재, 범위, 거절)
            has, status = number_status(r, ctx.settings, max_value)
        else:                                        # 예전 규칙 (null·oracle): 숫자로 읽혔고 신뢰도가 높으면
            has = True
            ok = val is not None and r.confidence >= auto_threshold(r, ctx.settings)
            status = "auto" if ok else "pending"
    return field_row(ctx, o, has_value=has, value_raw=raw, value_final=None if val is None else str(val), confidence=conf,
                     candidates=r.candidates if r else None, backend=r.backend if r else "ink", review_status=status)


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
        rows = apply_reviews(ctx, rows)
        upsert(ctx.con, "doc_field", rows)
        return {"fields": len(rows), "pending": sum(r["review_status"] == "pending" for r in rows)}

    def load_handwritten(self, ctx: PageContext, hw: list[CellObs]) -> list[dict]:
        inked = [o for o in hw if o.ink >= self.text_ink_min]
        filled = [o for o in inked if readable(o.cell)]
        recs = dict(zip([id(o) for o in filled], recognize(ctx, filled), strict=True))
        rows = []
        for o in hw:
            r = recs.get(id(o))
            if r is None and o.ink >= self.text_ink_min:   # 잉크는 있는데 읽지 않는 형식(소수·시각 …): 검수 대기 (tasks/0005 4.1)
                rows.append(unread_row(ctx, o))
            elif r is None:      # 잉크가 없으면 빈 셀로 확정
                rows.append(field_row(ctx, o, has_value=False, value_raw="", value_final="", confidence=1.0,
                                      candidates=None, backend="ink", review_status="auto"))
            else:
                if r.answer is not None and o.cell.kind == "handwritten_number":      # 숫자 인식기: 4.4 의 표
                    has, status = number_status(r, ctx.settings)
                else:                                                               # 예전 규칙 (null·oracle·글자 칸)
                    has = True
                    status = "auto" if bool(r.text) and r.confidence >= auto_threshold(r, ctx.settings) else "pending"
                rows.append(field_row(ctx, o, has_value=has, value_raw=r.text, value_final=r.text,
                                      confidence=r.confidence, candidates=r.candidates, backend=r.backend,
                                      review_status=status))
        return rows

    def finalize(self, con: sqlite3.Connection, site: SitePack, settings: Settings, dates: set[str] | None = None,
                 equipment: set[str] | None = None) -> dict:
        """날짜로(·장비로) 다시 계산하는 것 — 양식 간 교차검증, 계기의 연속성, 같은 날의 점검 행 (tasks/0007 4.8).
        dates·equipment 가 None 이면 전부 (모든 문서를 처리한 뒤). 아니면 그 날짜·장비만 — 문서 하나를 다시 처리한 직후 그 문서가
        있던·있는 날짜와 장비. 파이프라인은 등록된 핸들러 전부에 대해 부른다 (이번에 쪽을 적재한 핸들러만이 아니다). 커밋하지 않는다."""
        return {}

    def machine_final(self, row: dict) -> str | None:
        """기계만으로 정했을 때의 value_final. 검수를 다시 적용하기 전에 행을 기계 상태로 되돌릴 때 쓴다."""
        return row["value_raw"]

    def export_cells(self, template, fields: dict[str, dict]) -> tuple[dict[str, str], list[str]]:
        """내보내기(엑셀)에서 쪽의 칸 상태를 고쳐 말한다 (tasks/0008 4.2 — 양식의 뜻을 아는 것은 핸들러다). fields: 그 쪽의 최종
        doc_field 행 (field_id → 행). 돌려주는 값: (field_id → 상태 'empty' | 'present', 쪽의 머리에 적을 줄들).
        DB 를 읽지도 쓰지도 않는다. 기본은 아무것도 고치지 않는다."""
        return {}, []

    def on_review(self, con: sqlite3.Connection, site: SitePack, settings: Settings, field_id: str) -> None:
        """검수를 저장한 직후 호출된다: 이 필드로 만든 업무 행을 파이프라인을 다시 돌리지 않고 갱신한다.
        기본은 아무것도 하지 않는다 (업무 테이블이 없는 핸들러)."""
        return None


def readable(cell) -> bool:
    """인식기에 보내는 칸인가: 형식이 없거나(글자) integer 인 칸만. 소수·시각·계기 칸은 지금의 숫자 모델이 읽지 못한다 —
    보내지 않고, 잉크가 있으면 검수 대기다 (tasks/0005 4.1. 소수·시각을 읽는 것은 미룸 — tasks/0006 1절)."""
    return cell.fmt in (None, "integer")


def unread_row(ctx: PageContext, o: CellObs) -> dict:
    """잉크는 있지만 읽지 않은 칸: 값 있음 + 검수 대기, 기계 값 없음 (value_raw·value_final NULL — machine_final 과 같다)."""
    return field_row(ctx, o, has_value=True, value_raw=None, value_final=None, confidence=None, candidates=None,
                     backend="ink", review_status="pending")


def _printed_value(o: CellObs) -> str | None:
    v = o.cell.row_meta.get(o.cell.name)
    return None if v is None else str(v)
