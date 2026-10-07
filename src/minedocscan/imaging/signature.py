"""다시 스캔한 쪽을 가리는 서명 (tasks/0007 4.6).

정합 그림 → grid.binarize → 인쇄 자리를 0 으로 → 2 × 2 열기(점 잡음) → BLOCK × BLOCK px 칸마다 잉크 화소의 수. 같은 종이를 다시 스캔하면
손글씨가 같은 칸에 같은 양으로 남고, 같은 날의 다른 종이는 손글씨의 자리가 다르다. 두 서명의 비슷함은 코사인이다.
인쇄 자리: 템플릿에 인쇄 층이 있으면 그 마스크(Template.print_mask), 없으면 기준 이미지를 인쇄 층으로 본 마스크(printlayer.mask) —
기준 이미지로 쓴 그 종이 자신의 손글씨도 지워지므로 그 한 장은 서명이 0 에 가깝다 (다시 스캔해도 잡지 못한다 — 그대로 둔다).

  signature(aligned, print_mask)   칸마다 잉크 화소의 수 (uint8, 2차원) — 순수 함수, DB 를 모른다
  similarity(a, b)                 코사인 (0–1). 한쪽이라도 전부 0 이거나 모양이 다르면 None — 비교하지 않는다
  encode(sig) / decode(text)       doc_page_sig.sig 의 글자열 (SQLite·PostgreSQL 에서 같은 글자열)
"""
from __future__ import annotations

import base64

import cv2
import numpy as np

from .grid import binarize

BLOCK = 16              # 칸의 크기 (px, 200 dpi 템플릿 좌표 — 약 2 mm). 실제 3일치에서 이 크기로 같은 날 다른 종이 최대 0.66,
                        # 흔들어 다시 정합한 같은 종이 최소 0.91 (tasks/0007 1절 다)


def signature(aligned: np.ndarray, print_mask: np.ndarray | None, binary: np.ndarray | None = None) -> np.ndarray:
    """정합 그림의 손글씨 서명: 칸(BLOCK × BLOCK)마다 잉크 화소의 수 (0–255 — 한 칸 256 화소가 다 잉크인 일은 없다).
    binary: 이미 만든 grid.binarize(aligned) — 고쳐 쓰지 않는다 (복사해서 지운다)."""
    b = binarize(aligned) if binary is None else binary.copy()
    if print_mask is not None:
        b[print_mask] = 0                       # 이진화한 뒤 지운다 — 회색 그림을 칠하면 가장자리가 잉크가 된다 (ADR 0017)
    b = cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    h, w = b.shape
    hb, wb = h // BLOCK, w // BLOCK
    counts = (b[:hb * BLOCK, :wb * BLOCK] > 0).reshape(hb, BLOCK, wb, BLOCK).sum(axis=(1, 3))
    return np.minimum(counts, 255).astype(np.uint8)


def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    """두 서명의 코사인. 모양이 다르거나(다른 기준 그림) 한쪽이 전부 0 이면 None — 비교하지 않는다."""
    if a is None or b is None or a.shape != b.shape:
        return None
    x, y = a.astype(np.float64).ravel(), b.astype(np.float64).ravel()
    nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
    if nx == 0 or ny == 0:
        return None
    return float(np.dot(x, y) / (nx * ny))


def encode(sig: np.ndarray) -> str:
    """"높이x너비:" + 칸 값의 base64 (쪽마다 1–2만 자)."""
    h, w = sig.shape
    return f"{h}x{w}:" + base64.b64encode(np.ascontiguousarray(sig, dtype=np.uint8).tobytes()).decode("ascii")


def decode(text: str) -> np.ndarray:
    shape, data = text.split(":", 1)
    h, w = (int(v) for v in shape.split("x"))
    return np.frombuffer(base64.b64decode(data), dtype=np.uint8).reshape(h, w)
