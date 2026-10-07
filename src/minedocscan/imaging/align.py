"""정합(align): 스캔 페이지를 템플릿 기준 이미지 좌표계로 옮긴다.

ORB 특징점 → 비율 검정 → RANSAC 호모그래피. 인쇄된 양식(제목·표 괘선·고정 문구)이 특징점의
대부분을 차지하므로 수기 내용이 달라도 정합된다. 정합 품질은
  (1) RANSAC 인라이어 수
  (2) 정합 후 표(region)마다 괘선을 재검출해 템플릿 괘선과 비교한 오차(px, 중앙값)
두 가지로 기록하고, 기준을 넘지 못하면 ok=False 로 돌려 검수 큐로 보낸다.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace

import cv2
import numpy as np

from .grid import detect_grid_roi

MIN_INLIERS = 60            # 정합을 믿는 RANSAC 인라이어의 최소 수 (align_to_template 의 기본값 — 인쇄 층도 같은 값을 쓴다)


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
                      ref_features=None, ratio: float = 0.75, min_inliers: int = MIN_INLIERS,
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
    warped = warp_to_template(gray, H, ref_gray.shape)
    grid_err = grid_error(warped, regions)
    ok = inl >= min_inliers and grid_err <= max_grid_err
    return AlignResult(warped, H, len(good), inl, grid_err, ok)


def warp_to_template(gray: np.ndarray, homography, ref_shape: tuple[int, ...]) -> np.ndarray:
    """렌더링한 쪽 → 템플릿 좌표계 (기준 이미지 크기). 정합과 인쇄 층의 다시 펴기(tools/printlayer.py)가 같이 쓴다 —
    저장된 호모그래피(doc_page.homography)와 같은 해상도의 쪽이면 정합 그림과 바이트까지 같다."""
    h, w = ref_shape[:2]
    H = np.asarray(homography, dtype=np.float64)
    return cv2.warpPerspective(gray, H, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)


# ── 쪽의 방향 (tasks/0007 4.4) ─────────────────────────────────────────────────
# 양식은 B5 가로인데 급지 폭이 216 mm 인 스캐너에는 짧은 변부터 들어가 90° 돈 그림이 된다 (뒤집으면 270°·180°).
# ORB 와 호모그래피가 회전을 흡수해 돌아간 쪽도 정합은 되지만, 결과가 조용히 달라졌다 (실제 83쪽: 값 유무가 16–32칸 다름,
# 괘선 오차 +1.5–4 px). 첫 정합의 호모그래피에서 방향을 읽어(90° 단위에서 벗어난 각은 최대 2.05°) 정확히 되돌려 세운 뒤 다시
# 정합하면 정합 그림이 바로 선 쪽과 바이트까지 같았다 (249/249). 새 임계값은 없다 — 방향을 믿는 기준은 정합의 MIN_INLIERS 다.
ROTATIONS = (0, 90, 180, 270)


def orientation(homography) -> int:
    """정합의 호모그래피(쪽 → 템플릿)에서 쪽의 방향: 시계 방향으로 그만큼 돌리면 바로 선다 (0 | 90 | 180 | 270).
    회전각 atan2(H[1,0], H[0,0]) 을 90° 단위로 반올림한다 — 반시계 방향으로 90° 돈 쪽이 +90° 로 읽힌다."""
    H = np.asarray(homography, dtype=np.float64)
    ang = math.degrees(math.atan2(H[1, 0], H[0, 0]))
    return int(round(ang / 90.0)) % 4 * 90


def rotate_upright(gray: np.ndarray, rotation: int) -> np.ndarray:
    """쪽을 시계 방향으로 rotation 만큼 정확히 돌린다 (np.rot90 — 화소를 옮기기만 한다, 보간 없음)."""
    if rotation not in ROTATIONS:
        raise ValueError(f"방향은 {ROTATIONS} 중 하나: {rotation!r}")
    return np.ascontiguousarray(np.rot90(gray, -(rotation // 90))) if rotation else gray


def rotation_matrix(shape: tuple[int, ...], rotation: int) -> np.ndarray:
    """돌리기 전 쪽의 화소 좌표 → rotate_upright 로 세운 쪽의 화소 좌표 (3×3). shape: 돌리기 전 쪽의 (높이, 너비)."""
    h, w = shape[:2]
    M = np.eye(3)
    for _ in range(rotation // 90):                      # 시계 방향 90° 한 번: (x, y) → (h − 1 − y, x), 그다음 크기가 (w, h)
        M = np.array([[0.0, -1.0, h - 1.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]) @ M
        h, w = w, h
    return M


def align_upright(gray: np.ndarray, align: Callable, min_inliers: int = MIN_INLIERS) -> tuple:
    """쪽을 세워서 정합한다 (tasks/0007 4.4): align(그림) → AlignResult (또는 (AlignResult, 덤…) 튜플 — 동시 판의 묶음).
    첫 정합의 인라이어가 min_inliers 이상이고 방향이 0 이 아니면 쪽을 그만큼 정확히 세워 다시 정합하고 그 결과를 쓴다
    (통과 여부도 그 결과로). 바로 선 쪽은 정합을 한 번만 한다 (지금과 같은 횟수).
    돌려주는 값: (align 의 결과 — 그 AlignResult 의 homography 는 **돌리기 전 쪽 → 템플릿**으로 합성한 것, 정합 그림은 세운 쪽의 것,
    방향, 세운 쪽의 그림). 원본 해상도 크롭·인쇄 층의 다시 펴기는 저장된 호모그래피만으로 돈다 (방향을 몰라도)."""
    out = align(gray)
    ar = out[0] if isinstance(out, tuple) else out
    rotation = orientation(ar.homography) if ar.n_inliers >= min_inliers else 0
    if rotation == 0:
        return out, 0, gray
    upright = rotate_upright(gray, rotation)
    out = align(upright)
    first = out[0] if isinstance(out, tuple) else out
    if first.n_inliers:                                  # 정합이 됐으면 호모그래피를 원래 쪽 기준으로 (안 됐으면 단위 행렬 그대로)
        first = replace(first, homography=np.asarray(first.homography, dtype=np.float64)
                        @ rotation_matrix(gray.shape, rotation))
    out = (first, *out[1:]) if isinstance(out, tuple) else first
    return out, rotation, upright


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
