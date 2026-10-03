"""원본 해상도 크롭: 같은 셀을 더 촘촘히 뜬 것 (tasks/0002 4.5).

좌표계는 템플릿 좌표(200 dpi) 하나뿐이다. 정합·판정은 그대로 200 dpi 정합 이미지에서 하고, 여기서는
쪽마다 보관한 호모그래피(렌더링한 쪽 픽셀 → 템플릿 픽셀)를 써서 원본을 높은 해상도로 렌더링한 뒤 그 셀 영역만
템플릿 좌표의 out_scale 배 크기로 정합한다. 손글씨 인식기에 넘길 크롭은 원본 해상도가 나을 수 있다.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from .io import load_page


@lru_cache(maxsize=4)
def _render(path: str, mtime: float, page_no: int, dpi: int) -> np.ndarray:
    """원본의 한 쪽만 렌더링한다 (앞쪽을 차례로 렌더링하지 않는다 — 20쪽째가 2.5초 걸렸다).
    같은 쪽의 셀을 연달아 뜰 때를 위해 몇 장만 캐시한다 (mtime 은 캐시 무효화 키)."""
    return load_page(path, page_no, dpi)


def cell_from_source(source: str | Path, page_no: int, homography: np.ndarray, render_dpi: int,
                     bbox: tuple[int, int, int, int], pad: int = 0, out_scale: float = 1.0,
                     source_dpi: int = 300) -> np.ndarray:
    """원본 쪽에서 셀 하나를 템플릿 좌표의 out_scale 배 크기로 정합해 뜬다.

    homography 는 render_dpi 로 렌더링한 쪽의 픽셀을 템플릿 픽셀로 보내는 3×3 이다. PDF 는 source_dpi 로 다시 렌더링하고
    그 배율만큼 호모그래피를 보정한다. 이미지 파일로 들어온 문서는 원본이 곧 그 이미지라 배율이 1 이다.
    """
    source = Path(source)
    mtime = source.stat().st_mtime
    if source.suffix.lower() == ".pdf":
        img = _render(str(source), mtime, page_no, int(source_dpi))
        k = source_dpi / render_dpi                                  # 원본 픽셀 = 렌더링 픽셀 × k
    else:
        img = _render(str(source), mtime, 1, int(render_dpi))
        k = 1.0
    x0, y0, x1, y1 = bbox
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    s_inv = np.array([[1 / k, 0, 0], [0, 1 / k, 0], [0, 0, 1]], dtype=np.float64)       # 원본 → 렌더링 좌표
    t = np.array([[out_scale, 0, -x0 * out_scale], [0, out_scale, -y0 * out_scale], [0, 0, 1]], dtype=np.float64)
    m = t @ np.asarray(homography, dtype=np.float64) @ s_inv
    w, h = max(1, round((x1 - x0) * out_scale)), max(1, round((y1 - y0) * out_scale))
    return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)
