"""평가에 쓰는 통계 함수 — 비율의 구간과, 그 구간으로 목표를 뒷받침하는 데 필요한 표본 수.

평가(evaluate/fields.py)와 인식기의 보정(recognize/digits/calib.py)이 같이 쓴다. 인식기 쪽이 평가 쪽을 가져다 쓴다 (반대가 아니다).
"""
from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """이항 비율 k/n 의 윌슨 구간 (기본 95 %). n = 0 이면 (0, 1)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def zero_error_cells_for(target: float, z: float = 1.96) -> int:
    """오류 0 으로 윌슨 상한이 target 이하가 되려면 필요한 표본 수."""
    return math.ceil(z * z * (1 - target) / target)


def rate_with_ci(k: int, n: int) -> dict:
    """{"k", "n", "rate", "ci95"} — 분자·분모와 같이 낸다 (비율만 내지 않는다)."""
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": None if not n else round(k / n, 4), "ci95": [round(lo, 4), round(hi, 4)]}
