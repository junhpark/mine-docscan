"""셀 관측: 정합된 페이지에서 템플릿의 셀마다 크롭과 잉크 비율을 뽑는다. 모델을 쓰지 않는다."""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from ..forms.template import Cell, Template
from .grid import binarize


@dataclass
class CellObs:
    cell: Cell
    ink: float                    # 괘선을 지운 뒤의 잉크 픽셀 비율 (0~1)
    crop: np.ndarray = field(repr=False)


def remove_rules(binary: np.ndarray) -> np.ndarray:
    """셀 크롭 안에 남은 괘선 조각(긴 수평·수직 성분)을 지운다."""
    h, w = binary.shape
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, w // 2), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(15, h // 2)))
    rules = cv2.morphologyEx(binary, cv2.MORPH_OPEN, hk) | cv2.morphologyEx(binary, cv2.MORPH_OPEN, vk)
    rules = cv2.dilate(rules, np.ones((3, 3), np.uint8))
    return cv2.bitwise_and(binary, cv2.bitwise_not(rules))


def clean_cell(aligned_gray: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    """셀 하나의 이진 영상 (잉크=255). 괘선 조각과 점 잡음을 지운 상태."""
    x0, y0, x1, y1 = bbox
    b = remove_rules(binarize(aligned_gray[y0:y1, x0:x1]))
    return cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


def page_ink(gray: np.ndarray) -> float:
    """쪽 전체의 어두운 화소 비율: grid.binarize → 2 × 2 열기(점 잡음) → 0 이 아닌 화소의 비율. 빈 쪽을 가린다 (tasks/0007 4.5)."""
    b = cv2.morphologyEx(binarize(gray), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return float(np.count_nonzero(b)) / b.size if b.size else 0.0


def observe_cells(aligned_gray: np.ndarray, tpl: Template, print_mask: np.ndarray | None = None,
                  binary: np.ndarray | None = None) -> list[CellObs]:
    """칸마다 크롭(원래 그림)과 잉크 비율. print_mask(인쇄 마스크, bool — Template.print_mask)가 있으면 쪽의 이진 그림에서 그
    화소를 0 으로 한 그림을 한 번 만들어 role 표의 형식 있는 칸(Template.role_value_cell)의 잉크를 그것으로 잰다 (tasks/0006 4.3).
    회색 그림에서 인쇄를 흰색으로 칠한 뒤 이진화하지 않는다 — 적응 이진화가 칠한 자리의 가장자리를 잉크로 잡는다.
    그 밖의 칸과 크롭은 원래 그림 그대로다. print_mask=None 이면 예전과 같다. binary: 이미 만든 grid.binarize(aligned_gray)
    (파이프라인이 다시 스캔의 서명과 같이 쓴다 — 고쳐 쓰지 않는다)."""
    b = binarize(aligned_gray) if binary is None else binary
    bm = None
    if print_mask is not None:
        bm = b.copy()
        bm[print_mask] = 0
    out = []
    for c in tpl.cells() + tpl.field_cells():
        x0, y0, x1, y1 = c.bbox
        src = bm if bm is not None and tpl.role_value_cell(c) else b
        sub = remove_rules(src[y0:y1, x0:x1])
        sub = cv2.morphologyEx(sub, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        ink = float((sub > 0).mean()) if sub.size else 0.0
        out.append(CellObs(c, ink, aligned_gray[y0:y1, x0:x1]))
    return out
