"""인식(recognize) 단계의 백엔드 인터페이스.

파이프라인은 이 인터페이스만 안다. 구현체(로컬 VLM, 상용 OCR, 숫자 전용 인식기 …)는 설정으로 고르고,
같은 평가셋에서 수치로 비교해 바꾼다. 입력은 셀 크롭과 문맥, 출력은 텍스트·신뢰도·후보 목록이다.

크롭의 규격(imaging/cropspec.CropSpec)은 백엔드가 선언한다 (tasks/0003 4.1):
  crop_spec = CropSpec(...)              속성으로 — 모든 칸에 그 규격
  crop_spec_for(kind) -> CropSpec|None   메서드로 — 칸 종류마다 (종류별로 나눠 주는 ByKindRecognizer 가 쓴다)
선언하지 않으면 DEFAULT_SPEC(정합 이미지, 칸 그대로)을 받는다 — null·oracle 이 그렇다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass
class CellContext:
    template: str
    region: str
    field_name: str                  # remark, trips_day, date_line …
    kind: str                        # handwritten_text | handwritten_number
    row_key: str                     # 이 셀이 속한 행 (장비, 광종|편 …) — 템플릿에서 확정
    work_date: str | None = None     # 문서 날짜 (YYYY-MM-DD)
    page_id: str = ""
    source: str = ""                 # "<파일명(확장자 제외)>#<페이지 번호>" — 사람이 읽는 출처
    choices: list[str] = field(default_factory=list)   # 닫힌 집합이면 그 목록 (예: 운전자 명단)
    hints: list[str] = field(default_factory=list)     # 같은 행의 최근 값·반복 문구 (문구 DB)
    field_id: str = ""               # doc_field 의 키 (시험·진단용. 인식기가 값을 정하는 데 쓰지 않는다)


@dataclass
class Recognition:
    text: str
    confidence: float                # 0~1. 보정되지 않은 값이면 백엔드 문서에 그렇게 적는다
    candidates: list[str] = field(default_factory=list)
    backend: str = ""


class Recognizer(Protocol):
    name: str

    def recognize(self, crops: list[np.ndarray], contexts: list[CellContext]) -> list[Recognition]: ...


def spec_for(recognizer, kind: str):
    """그 백엔드가 그 종류의 칸에 원하는 크롭 규격. 선언이 없으면 DEFAULT_SPEC."""
    from ..imaging.cropspec import DEFAULT_SPEC

    fn = getattr(recognizer, "crop_spec_for", None)
    spec = fn(kind) if callable(fn) else getattr(recognizer, "crop_spec", None)
    return spec or DEFAULT_SPEC
