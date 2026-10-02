"""정합(align): 스캔 페이지를 템플릿 기준 이미지 좌표계로 옮긴다.

ORB 특징점 → 비율 검정 → RANSAC 호모그래피. 인쇄된 양식(제목·표 괘선·고정 문구)이 특징점의
대부분을 차지하므로 수기 내용이 달라도 정합된다. 정합 품질은
  (1) RANSAC 인라이어 수
  (2) 정합 후 표(region)마다 괘선을 재검출해 템플릿 괘선과 비교한 오차(px, 중앙값)
두 가지로 기록하고, 기준을 넘지 못하면 ok=False 로 돌려 검수 큐로 보낸다.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .grid import detect_grid_roi


@dataclass
class AlignResult:
    warped: np.ndarray          # 템플릿 좌표계로 옮긴 회색조 이미지
    homography: np.ndarray
    n_matches: int
    n_inliers: int
    grid_err_px: float          # 괘선 재검출 오차(px). 낮을수록 좋음
    ok: bool


def orb_features(gray: np.ndarray, n: int = 6000):
    orb = cv2.ORB_create(nfeatures=n, scaleFactor=1.2, nlevels=8, edgeThreshold=15, patchSize=31)
    return orb.detectAndCompute(gray, None)


def align_to_template(gray: np.ndarray, ref_gray: np.ndarray, regions: list[dict],
                      ref_features=None, ratio: float = 0.75, min_inliers: int = 60,
                      max_grid_err: float = 6.0) -> AlignResult:
    """regions: 템플릿의 표 목록 (각각 grid.ys / grid.xs). ref_features: 기준 이미지의 ORB 결과(캐시)."""
    k1, d1 = orb_features(gray)
    k2, d2 = ref_features if ref_features is not None else orb_features(ref_gray)
    if d1 is None or d2 is None:
        return AlignResult(gray, np.eye(3), 0, 0, float("inf"), False)
    knn = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
    good = [m for m, n in (p for p in knn if len(p) == 2) if m.distance < ratio * n.distance]
    if len(good) < 12:
        return AlignResult(gray, np.eye(3), len(good), 0, float("inf"), False)
    src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    inl = int(mask.sum()) if mask is not None else 0
    if H is None:
        return AlignResult(gray, np.eye(3), len(good), inl, float("inf"), False)
    h, w = ref_gray.shape
    warped = cv2.warpPerspective(gray, H, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)
    grid_err = grid_error(warped, regions)
    ok = inl >= min_inliers and grid_err <= max_grid_err
    return AlignResult(warped, H, len(good), inl, grid_err, ok)


def grid_error(warped: np.ndarray, regions: list[dict]) -> float:
    """표마다 그 영역 안에서 괘선을 재검출해 템플릿 괘선과의 오차를 잰다. 표 중 가장 나쁜 값을 돌려준다."""
    h, w = warped.shape
    errs = []
    for reg in regions:
        rys, rxs = reg["grid"]["ys"], reg["grid"]["xs"]
        roi = (max(0, min(rxs) - 12), max(0, min(rys) - 12), min(w, max(rxs) + 12), min(h, max(rys) + 12))
        ys, xs = detect_grid_roi(warped, roi)
        errs.append(_line_error(ys, rys) if ys else float("inf"))
        errs.append(_line_error(xs, rxs) if xs else float("inf"))
    return float(max(errs)) if errs else float("inf")


def _line_error(found: list[int], ref: list[int]) -> float:
    """템플릿 괘선마다 가장 가까운 재검출 괘선까지의 거리의 중앙값.

    평균이 아니라 중앙값인 이유: 스캔 가장자리에서 잘린 괘선 한두 개가 전체 판정을 뒤집지 않게 하려고.
    """
    f = np.array(found)
    return float(np.median([np.min(np.abs(f - r)) for r in ref]))
