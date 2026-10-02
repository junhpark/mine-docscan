"""인식(recognize) 단계의 백엔드 인터페이스.

파이프라인은 이 인터페이스만 안다. 구현체(로컬 VLM, 상용 OCR, 숫자 전용 인식기 …)는 설정으로 고르고,
같은 평가셋에서 수치로 비교해 바꾼다. 입력은 셀 크롭과 문맥, 출력은 텍스트·신뢰도·후보 목록이다.
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


@dataclass
class Recognition:
    text: str
    confidence: float                # 0~1. 보정되지 않은 값이면 백엔드 문서에 그렇게 적는다
    candidates: list[str] = field(default_factory=list)
    backend: str = ""


class Recognizer(Protocol):
    name: str

    def recognize(self, crops: list[np.ndarray], contexts: list[CellContext]) -> list[Recognition]: ...
