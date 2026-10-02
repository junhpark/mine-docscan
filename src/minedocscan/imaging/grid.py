"""표 격자(수평·수직 괘선) 검출 — 기준 페이지에서 템플릿을 뽑을 때와, 정합 품질 확인에 쓴다."""
from __future__ import annotations

import cv2
import numpy as np


def binarize(gray: np.ndarray) -> np.ndarray:
    """잉크=255, 배경=0 인 이진 영상."""
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 15)


def line_masks(binary: np.ndarray, min_h_frac: float = 0.25, min_v_frac: float = 0.10):
    """긴 수평선/수직선만 남긴 마스크 두 장을 돌려준다."""
    h, w = binary.shape
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(w * min_h_frac)), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, int(h * min_v_frac))))
    horiz = cv2.morphologyEx(binary, cv2.MORPH_OPEN, hk)
    vert = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vk)
    return horiz, vert


def _cluster_positions(profile: np.ndarray, thresh: float, min_gap: int) -> list[int]:
    """1차원 프로파일에서 임계값을 넘는 구간의 중심을 모아 위치 목록으로."""
    idx = np.where(profile > thresh)[0]
    if len(idx) == 0:
        return []
    groups, cur = [], [idx[0]]
    for i in idx[1:]:
        if i - cur[-1] <= min_gap:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)
    return [int(round(float(np.mean(g)))) for g in groups]


def detect_grid(gray: np.ndarray, roi: tuple[int, int, int, int] | None = None):
    """표 영역(roi=x0,y0,x1,y1)에서 수평선 y좌표 목록과 수직선 x좌표 목록을 검출한다."""
    if roi is not None:
        x0, y0, x1, y1 = roi
        sub = gray[y0:y1, x0:x1]
    else:
        x0 = y0 = 0
        sub = gray
    b = binarize(sub)
    horiz, vert = line_masks(b)
    hprof = (horiz > 0).sum(axis=1) / horiz.shape[1]
    vprof = (vert > 0).sum(axis=0) / vert.shape[0]
    ys = _cluster_positions(hprof, 0.20, 4)
    xs = _cluster_positions(vprof, 0.20, 4)
    return [y + y0 for y in ys], [x + x0 for x in xs], horiz, vert


def detect_grid_roi(gray: np.ndarray, roi: tuple[int, int, int, int], h_thresh: float = 0.4,
                    v_thresh: float = 0.3):
    """표 하나의 영역(roi) 안에서 괘선을 검출한다. 임계값은 roi 폭·높이에 대한 비율이라
    가로 양식의 짧은 괘선(옆에 다른 표가 있는 경우)도 잡힌다."""
    x0, y0, x1, y1 = roi
    sub = gray[y0:y1, x0:x1]
    b = binarize(sub)
    horiz, vert = line_masks(b, min_h_frac=0.15, min_v_frac=0.10)
    hprof = (horiz > 0).sum(axis=1) / horiz.shape[1]
    vprof = (vert > 0).sum(axis=0) / vert.shape[0]
    return ([y + y0 for y in _cluster_positions(hprof, h_thresh, 4)],
            [x + x0 for x in _cluster_positions(vprof, v_thresh, 4)])
