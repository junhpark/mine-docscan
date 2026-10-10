"""다시 스캔한 쪽을 가리는 서명 (tasks/0007 4.6, 판 2 — tasks/0010 4.3).

정합 그림 → grid.binarize → 지울 자리를 0 으로 → 2 × 2 열기(점 잡음) → BLOCK × BLOCK px 칸마다 잉크 화소의 수. 같은 종이를 다시 스캔하면
손글씨가 같은 칸에 같은 양으로 남고, 같은 날의 다른 종이는 손글씨의 자리가 다르다. 두 서명의 비슷함은 코사인이다.
지울 자리: 인쇄(템플릿에 인쇄 층이 있으면 그 마스크 — Template.print_mask, 없으면 기준 이미지를 인쇄 층으로 본 마스크 — printlayer.mask)와
표 밖 필드의 칸을 WIDEN × WIDEN 으로 넓힌 것 (Template.signature_mask — 템플릿마다 한 번 만든다). 기준 이미지로 쓴 그 종이 자신의
손글씨도 지워지므로 그 한 장은 서명이 0 에 가깝다 (다시 스캔해도 잡지 못한다 — 그대로 둔다).

판 (VERSION): 서명의 글자열 앞에 적는다 (`2|높이x너비:…` — 판이 없는 옛 글자열은 판 1). 판이 다른 서명은 견주지 않는다 — decode 가
None 을 주고 similarity 가 None 이다 (다시 스캔으로 보지 않는다). 옛 판의 쪽은 run --fresh 로 다시 만든다 (report·info 가 그 수를 알린다).

  signature(aligned, mask)   칸마다 잉크 화소의 수 (uint8, 2차원) — 순수 함수, DB 를 모른다
  similarity(a, b)           코사인 (0–1). 한쪽이라도 None·전부 0 이거나 모양이 다르면 None — 비교하지 않는다
  encode(sig) / decode(text) doc_page_sig.sig 의 글자열 (SQLite·PostgreSQL 에서 같은 글자열). decode 는 지금의 판이 아니면 None
  version_of(text)           글자열의 판 (앞머리가 없으면 1)
  widen(mask)                서명에서 지우는 자리를 넓힌다 (Template.signature_mask 가 부른다)
"""
from __future__ import annotations

import base64

import cv2
import numpy as np

from .grid import binarize

BLOCK = 16              # 칸의 크기 (px, 200 dpi 템플릿 좌표 — 약 2 mm). 실제 3일치에서 이 크기(표 밖 필드도 지운 서명)로 같은 날
                        # 다른 종이 최대 0.593, 흔들어 다시 정합한 같은 종이 중앙 0.99 — 그러나 2–3 % 는 0.80 아래 (tasks/0008 1절 라)
VERSION = 2
# 지울 자리를 넓히는 폭 (판 2 — tasks/0010 4.3): 판 1 은 인쇄 마스크 그대로라 정합이 1–2 px 어긋나면 괘선·인쇄 글자가 마스크 밖으로 밀려
# 나와 서명을 흔들었다 — 실제 81쪽의 정합 그림을 (2, 1) px 옮기면 같은 그림끼리 최소 0.608, 3 px 0.316, 0.2° 돌리면 0.810 (기준 0.80).
# 손글씨는 1–2 px 옮겨도 같은 칸에 남는다. 합성(scripts/sig_probe.py — 다시 스캔 묶음 17쪽)에서 넓히는 폭마다 (2, 1) px + 0.2° 의 최소:
# 판 1 0.597, 5 × 5 0.946, 7 × 7 0.952, 9 × 9 0.952 — 셋 다 기준(4.3 나)을 넘고 같은 날 다른 종이의 최대는 그대로(0.594). 더 크게
# 어긋나면 갈린다: 0.4° 에서 7 × 7 0.786, 9 × 9 0.920 / (3, 2) px + 0.3° 에서 0.696, 0.867. 9 × 9 (±4 px) 는 실제 정합의 괘선 오차가
# 양식에 따라 4 px 까지 나오는 것을 덮는다. 긴 선(41 px)을 지우는 것은 혼자서는 기준을 넘지 못했고((2, 1) px + 0.2° 의 5 % 0.894),
# 넓힌 마스크와 같이 써도 나아지지 않고 쪽 하나가 1.4–1.6배 느렸다 — 고른 것과 버린 것의 수치는 ADR 0020
WIDEN = 9


def widen(mask: np.ndarray) -> np.ndarray:
    """지울 자리(bool)를 WIDEN × WIDEN 으로 넓힌다."""
    return cv2.dilate(mask.astype(np.uint8), np.ones((WIDEN, WIDEN), np.uint8)).astype(bool)


def signature(aligned: np.ndarray | None, mask: np.ndarray | None, binary: np.ndarray | None = None) -> np.ndarray:
    """정합 그림의 손글씨 서명: 칸(BLOCK × BLOCK)마다 잉크 화소의 수 (0–255 — 한 칸 256 화소가 다 잉크인 일은 없다).
    mask: 지울 자리 (Template.signature_mask — 넓힌 것). binary: 이미 만든 grid.binarize(aligned) — 고쳐 쓰지 않는다 (복사해서 지운다)."""
    b = binarize(aligned) if binary is None else binary.copy()
    if mask is not None:
        b[mask] = 0                             # 이진화한 뒤 지운다 — 회색 그림을 칠하면 가장자리가 잉크가 된다 (ADR 0017)
    b = cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    h, w = b.shape
    hb, wb = h // BLOCK, w // BLOCK
    counts = (b[:hb * BLOCK, :wb * BLOCK] > 0).reshape(hb, BLOCK, wb, BLOCK).sum(axis=(1, 3))
    return np.minimum(counts, 255).astype(np.uint8)


def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    """두 서명의 코사인. 한쪽이 None(옛 판 — decode)이거나 모양이 다르거나(다른 기준 그림) 전부 0 이면 None — 비교하지 않는다."""
    if a is None or b is None or a.shape != b.shape:
        return None
    x, y = a.astype(np.float64).ravel(), b.astype(np.float64).ravel()
    nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
    if nx == 0 or ny == 0:
        return None
    return float(np.dot(x, y) / (nx * ny))


def encode(sig: np.ndarray) -> str:
    """"판|높이x너비:" + 칸 값의 base64 (쪽마다 1–2만 자)."""
    h, w = sig.shape
    return f"{VERSION}|{h}x{w}:" + base64.b64encode(np.ascontiguousarray(sig, dtype=np.uint8).tobytes()).decode("ascii")


def version_of(text: str) -> int:
    """글자열의 판 — 앞머리('2|')가 없으면 판 1 (tasks/0010 전의 서명)."""
    head = text.split(":", 1)[0]
    return int(head.split("|", 1)[0]) if "|" in head else 1


def decode(text: str) -> np.ndarray | None:
    """글자열 → 서명. 지금의 판(VERSION)이 아니면 None — 판이 다른 서명은 견주지 않는다."""
    if version_of(text) != VERSION:
        return None
    shape, data = text.split(":", 1)
    h, w = (int(v) for v in shape.split("|", 1)[-1].split("x"))
    return np.frombuffer(base64.b64decode(data), dtype=np.uint8).reshape(h, w)
