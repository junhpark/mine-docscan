"""체크 표시(✓) 판정: 나란한 두 칸(예: 장비이상 유 / 무) 중 어느 쪽에 표시했는가.

현장의 ✓ 는 왼쪽 칸에서 시작해 경계선을 넘어 오른쪽 칸까지 길게 그려지는 일이 많다. 그래서 잉크량
비교나 꼭짓점 위치로는 틀린다. 실제 데이터에서 맞았던 규칙은 다음과 같다.

  1. 왼쪽 칸 안에, 칸의 왼쪽 가장자리에서 시작하지 않는 잉크 덩어리가 있으면 → 왼쪽 선택
     (왼쪽 가장자리에서 들어온 잉크는 그 앞 열의 글씨가 넘친 것이므로 제외)
  2. 아니고 오른쪽 칸에 잉크 덩어리가 있으면 → 오른쪽 선택
  3. 둘 다 없으면 판정 불가
  4. 페이지의 행 대부분(80% 이상)이 판정 불가면 그날은 그 열을 쓰지 않은 것 → 전부 column_unused
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .cells import CellObs, clean_cell


@dataclass
class MarkDecision:
    choice: str | None        # "first" | "second" | None
    status: str               # ok | empty | column_unused
    ink_first: float
    ink_second: float


def _components(binary: np.ndarray, min_area: int):
    n, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    return [stats[i] for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_area]


def decide_mark_pairs(aligned_gray: np.ndarray, obs: list[CellObs], first: str, second: str,
                      min_area: int = 15, edge_px: int = 4, unused_frac: float = 0.8) -> dict[int, MarkDecision]:
    """행마다 first / second 두 칸 중 어디에 표시했는지 정한다. first 는 왼쪽 칸의 컬럼 이름."""
    by_row: dict[int, dict[str, CellObs]] = {}
    for o in obs:
        if o.cell.kind == "checkmark" and o.cell.name in (first, second):
            by_row.setdefault(o.cell.row, {})[o.cell.name] = o
    result: dict[int, MarkDecision] = {}
    for row, d in by_row.items():
        if first not in d or second not in d:
            continue
        of, os_ = d[first], d[second]
        first_marks = [s for s in _components(clean_cell(aligned_gray, of.cell.bbox), min_area)
                       if s[cv2.CC_STAT_LEFT] > edge_px]
        if first_marks:
            result[row] = MarkDecision("first", "ok", of.ink, os_.ink)
            continue
        second_marks = _components(clean_cell(aligned_gray, os_.cell.bbox), min_area * 2)
        if second_marks:
            result[row] = MarkDecision("second", "ok", of.ink, os_.ink)
        else:
            result[row] = MarkDecision(None, "empty", of.ink, os_.ink)
    n_empty = sum(1 for v in result.values() if v.status == "empty")
    if result and n_empty >= unused_frac * len(result):
        result = {r: MarkDecision(None, "column_unused", v.ink_first, v.ink_second) for r, v in result.items()}
    return result
