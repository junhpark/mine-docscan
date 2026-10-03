"""원본 해상도: 같은 셀을 더 촘촘히 뜨기 위해 원본 쪽을 높은 해상도로 렌더링한다 (tasks/0002 4.5).

좌표계는 템플릿 좌표(200 dpi) 하나뿐이다. 정합·판정은 그대로 200 dpi 정합 이미지에서 하고, 원본 해상도 크롭은
쪽마다 보관한 호모그래피(렌더링한 쪽 픽셀 → 템플릿 픽셀)를 써서 그 셀 영역만 정합한다 — imaging/cropspec.py.
여기는 원본 쪽을 렌더링하는 일만 한다 (몇 장만 캐시).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

from .io import load_page


@lru_cache(maxsize=4)
def _render(path: str, mtime: float, page_no: int, dpi: int, damaged: str = "fail") -> np.ndarray:
    """원본의 한 쪽만 렌더링한다 (앞쪽을 차례로 렌더링하지 않는다 — 20쪽째가 2.5초 걸렸다).
    같은 쪽의 셀을 연달아 뜰 때를 위해 몇 장만 캐시한다 (mtime 은 캐시 무효화 키)."""
    return load_page(path, page_no, dpi, damaged)


def render_source(source: str | Path, page_no: int, render_dpi: int, source_dpi: int = 300,
                  damaged: str = "fail") -> tuple[np.ndarray, float]:
    """원본 쪽 그림과 k(= 원본 픽셀 / 렌더링 픽셀). PDF 는 source_dpi 로 다시 렌더링하고, 이미지 파일로 들어온
    문서는 원본이 곧 그 이미지라 k = 1 이다 (파이프라인도 이미지를 그 해상도 그대로 썼다)."""
    source = Path(source)
    mtime = source.stat().st_mtime
    if source.suffix.lower() == ".pdf":
        return _render(str(source), mtime, page_no, int(source_dpi), damaged), source_dpi / render_dpi
    return _render(str(source), mtime, 1, int(render_dpi), damaged), 1.0
