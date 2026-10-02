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


def observe_cells(aligned_gray: np.ndarray, tpl: Template) -> list[CellObs]:
    b = binarize(aligned_gray)
    out = []
    for c in tpl.cells() + tpl.field_cells():
        x0, y0, x1, y1 = c.bbox
        sub = remove_rules(b[y0:y1, x0:x1])
        sub = cv2.morphologyEx(sub, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        ink = float((sub > 0).mean()) if sub.size else 0.0
        out.append(CellObs(c, ink, aligned_gray[y0:y1, x0:x1]))
    return out
