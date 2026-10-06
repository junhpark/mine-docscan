"""인쇄 층 (tasks/0006 4.2·4.3): 같은 양식의 정합 그림 여러 장에서 화소마다 밝기의 높은 백분위 → 손글씨가 빠진 빈 양식.

인쇄는 모든 쪽에서 같은 자리에 있고 손글씨는 쪽마다 다르다. 쪽마다 잉크를 1 px 넓힌 뒤(회색조 erode 3×3 — 정합의 1 px 흔들림)
화소마다 밝기의 백분위(기본 75)를 잡으면, 쪽의 대부분에서 어두운 화소(인쇄)만 남는다. 새 임계값은 없다 — 이진화는 값 유무와
같은 `grid.binarize` 다.

  estimate(pages)   인쇄 층 (회색조 uint8). 같은 쪽·같은 백분위면 바이트까지 같다
  binary(layer)     인쇄 화소 (넓히지 않은 것) — 요약의 "덮인 비율", 미리보기의 색
  mask(layer)       인쇄 마스크 = binary 를 2 px 넓힌 것 — 값 유무를 잴 때 지우는 자리 (4.3, 단계 3)
  sha(img)          화소의 해시 (PNG 파일의 바이트가 아니다 — 다른 도구로 다시 저장해도 같다)
  coverage(m, bbox) 칸마다 마스크(또는 이진 층)에 덮인 비율

DB 를 모른다. 쪽을 고르고 펴는 일은 tools/printlayer.py 가 한다.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

import cv2
import numpy as np

from .grid import binarize

ROW_BLOCK = 64          # 백분위를 이만큼의 행씩 잰다 — 40장을 통째로 float64 로 쌓으면 세로 양식에서 메모리가 약 0.5 GB 더 든다
MASK_GROW = 2           # 인쇄 마스크를 넓히는 폭 (px). 실데이터에서 1–3 이 같았고 합성에서도 0–3 이 같았다 (tasks/0006 1절, 9절)


def estimate(pages: Iterable[np.ndarray], percentile: float = 75) -> np.ndarray:
    """템플릿 좌표로 편 회색조 그림들(같은 크기) → 인쇄 층 (uint8). 쪽마다 erode 3×3 뒤 화소마다 밝기의 백분위."""
    if not 0 <= percentile <= 100:
        raise ValueError(f"백분위는 0–100: {percentile}")
    eroded = []
    for g in pages:
        g = np.asarray(g)
        if g.dtype != np.uint8 or g.ndim != 2:
            raise ValueError("인쇄 층은 회색조 uint8 그림으로 만든다")
        if eroded and g.shape != eroded[0].shape:
            raise ValueError(f"쪽의 크기가 다릅니다: {g.shape} ≠ {eroded[0].shape}")
        eroded.append(cv2.erode(g, np.ones((3, 3), np.uint8)))
    if not eroded:
        raise ValueError("쪽이 없습니다")
    h, w = eroded[0].shape
    out = np.empty((h, w), np.uint8)
    for y0 in range(0, h, ROW_BLOCK):
        block = np.stack([e[y0:y0 + ROW_BLOCK] for e in eroded])
        p = np.percentile(block, percentile, axis=0)
        out[y0:y0 + ROW_BLOCK] = np.clip(np.rint(p), 0, 255).astype(np.uint8)
    return out


def binary(layer: np.ndarray) -> np.ndarray:
    """인쇄 화소 (bool) — 인쇄 층을 값 유무와 같은 `grid.binarize` 로 이진화한 것. 넓히지 않는다."""
    return binarize(layer) > 0


def mask(layer: np.ndarray) -> np.ndarray:
    """인쇄 마스크 (bool, 4.3): binary 를 MASK_GROW px 넓힌 것. 값 유무를 잴 때 이 화소를 잉크에서 뺀다 (단계 3)."""
    k = 2 * MASK_GROW + 1
    return cv2.dilate(binary(layer).astype(np.uint8), np.ones((k, k), np.uint8)) > 0


def sha(img: np.ndarray) -> str:
    """print_sha: sha256(높이·너비 + 화소 바이트)의 앞 16자."""
    img = np.ascontiguousarray(img)
    h, w = img.shape[:2]
    return hashlib.sha256(f"{h}x{w}:".encode() + img.tobytes()).hexdigest()[:16]


def coverage(layer_mask: np.ndarray, boxes: Iterable[tuple[int, int, int, int]]) -> list[float]:
    """칸(bbox x0,y0,x1,y1 — 템플릿 좌표)마다 마스크가 True 인 화소의 비율. 그림 밖은 자르고, 남는 것이 없으면 0."""
    h, w = layer_mask.shape[:2]
    out = []
    for x0, y0, x1, y1 in boxes:
        sub = layer_mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
        out.append(float(sub.mean()) if sub.size else 0.0)
    return out
