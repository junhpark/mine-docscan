"""합성 손글씨의 획 정의 — OpenCV 판에 상관없이 같은 글자를 그린다.

예전에는 OpenCV 내장 글꼴(Hershey)로 숫자를 그렸는데, OpenCV 5.0 에서 내장 글꼴의 모양이 완전히 바뀌었다. 그래서 5.0 의 글꼴로
학습한 시험용 모델이 4.x 에서 만든 합성 칸을 못 읽었다 (자동 적재 오류율 47 %). 이제 글자는 여기 적은 획(점 목록)을 직선으로
이어 그린다 — `cv2.polylines` 만 쓰므로 판이 바뀌어도 같다.

좌표: 글자 상자 안의 단위 좌표, x 는 0(왼쪽)부터 약 0.6, y 는 0(위)부터 1(아래 — 기준선). 곡선은 호를 촘촘한 점으로 펼친다.
한 글자에 모양이 여러 가지인 것(1 의 받침, 4 의 열린·닫힌 꼴, 7 의 가로줄, 9 의 꼬리)은 쓰는 사람(style["variants"])마다 하나를 고른다.
메모에 쓰는 영문 소문자는 대충 닮은 꼴이면 된다 — 메모는 "이 칸의 값이 아닌 것"의 예일 뿐이다.
"""
from __future__ import annotations

import contextlib
import math

import cv2
import numpy as np

Stroke = list[tuple[float, float]]


def _arc(cx: float, cy: float, rx: float, ry: float, a0: float, a1: float, n: int | None = None) -> Stroke:
    """중심 (cx, cy), 반지름 (rx, ry) 의 호. 각도는 도, 0 = 오른쪽, 90 = 아래 (y 가 아래로 자란다)."""
    n = n or max(6, int(abs(a1 - a0) / 12))
    return [(cx + rx * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + ry * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


# ── 숫자 ───────────────────────────────────────────────────────────────────
DIGITS: dict[str, list[list[Stroke]]] = {
    "0": [[_arc(0.28, 0.5, 0.26, 0.49, -90, 270)],
          [_arc(0.28, 0.5, 0.24, 0.49, -100, 265)]],
    "1": [[[(0.3, 0.0), (0.3, 1.0)]],
          [[(0.3, 0.0), (0.29, 1.0)]],
          # 깃발 달린 1 은 넣지 않는다: 작게 줄이고 기울이면 짧은 가로획의 7 과 구별되지 않아 사람도 못 읽는 합성 칸이 생겼다
          [[(0.3, 0.0), (0.3, 1.0)], [(0.12, 1.0), (0.48, 1.0)]]],
    "2": [[_arc(0.28, 0.27, 0.24, 0.25, 195, 380) + [(0.03, 1.0), (0.56, 1.0)]],
          [_arc(0.27, 0.28, 0.23, 0.26, 200, 370) + [(0.08, 0.95), (0.05, 1.0), (0.56, 0.97)]]],
    "3": [[_arc(0.26, 0.26, 0.22, 0.24, 200, 450) + _arc(0.26, 0.74, 0.27, 0.26, -90, 160)],
          [[(0.04, 0.0), (0.5, 0.0), (0.22, 0.42)] + _arc(0.26, 0.72, 0.27, 0.28, -80, 160)]],
    "4": [[[(0.42, 1.0), (0.42, 0.0), (0.02, 0.68), (0.57, 0.68)]],
          [[(0.08, 0.0), (0.04, 0.6), (0.57, 0.6)], [(0.43, 0.18), (0.43, 1.0)]]],
    "5": [[[(0.52, 0.0), (0.1, 0.0), (0.07, 0.45)] + _arc(0.27, 0.7, 0.27, 0.3, -130, 160)],
          [[(0.08, 0.02), (0.06, 0.44)] + _arc(0.27, 0.7, 0.26, 0.3, -130, 160), [(0.08, 0.02), (0.52, 0.0)]]],
    "6": [[[(0.5, 0.03), (0.3, 0.14), (0.12, 0.36), (0.04, 0.62)] + _arc(0.28, 0.74, 0.24, 0.26, 180, 540)],
          [_arc(0.45, 0.62, 0.42, 0.6, 250, 180, 10) + _arc(0.28, 0.75, 0.23, 0.24, 180, 540)]],
    "7": [[[(0.0, 0.0), (0.64, 0.0), (0.22, 1.0)]],                     # 윗가로획이 길어야 1 과 갈린다
          [[(0.0, 0.06), (0.64, 0.0), (0.24, 1.0)], [(0.18, 0.52), (0.56, 0.5)]]],
    "8": [[_arc(0.28, 0.25, 0.2, 0.24, 90, 450) + _arc(0.28, 0.73, 0.25, 0.26, -90, 270)],
          [_arc(0.28, 0.26, 0.21, 0.25, 0, 360), _arc(0.28, 0.74, 0.25, 0.25, 0, 360)]],
    "9": [[_arc(0.28, 0.29, 0.24, 0.28, 0, 360) + [(0.52, 0.29), (0.48, 1.0)]],
          [_arc(0.28, 0.3, 0.24, 0.28, 10, 370) + _arc(0.15, 0.32, 0.37, 0.68, 0, 95, 8)]],
}

# ── 메모용 소문자 (x 높이 0.45–1.0, 올라가는 획은 0 부터) ──────────────────────
_O = _arc(0.22, 0.73, 0.2, 0.27, 0, 360)
LETTERS: dict[str, list[Stroke]] = {
    "a": [_arc(0.22, 0.73, 0.2, 0.27, -20, 340), [(0.42, 0.48), (0.42, 1.0)]],
    "b": [[(0.04, 0.0), (0.04, 1.0)], _arc(0.22, 0.73, 0.18, 0.27, 180, 540)],
    "c": [_arc(0.24, 0.73, 0.2, 0.27, -40, 220)],
    "d": [_arc(0.2, 0.73, 0.18, 0.27, 0, 360), [(0.38, 0.0), (0.38, 1.0)]],
    "e": [[(0.04, 0.72), (0.42, 0.72)] + _arc(0.23, 0.73, 0.2, 0.27, 0, 300)],
    "f": [_arc(0.3, 0.12, 0.12, 0.12, -10, -180) + [(0.18, 1.0)], [(0.04, 0.48), (0.36, 0.48)]],
    "h": [[(0.04, 0.0), (0.04, 1.0)], _arc(0.21, 0.66, 0.17, 0.18, 180, 360) + [(0.38, 1.0)]],
    "i": [[(0.12, 0.48), (0.12, 1.0)], [(0.12, 0.24), (0.13, 0.27)]],
    "k": [[(0.04, 0.0), (0.04, 1.0)], [(0.36, 0.46), (0.06, 0.74), (0.38, 1.0)]],
    "l": [[(0.1, 0.0), (0.1, 1.0)]],
    "m": [[(0.03, 0.46), (0.03, 1.0)], _arc(0.15, 0.62, 0.12, 0.15, 180, 360) + [(0.27, 1.0)],
          _arc(0.39, 0.62, 0.12, 0.15, 180, 360) + [(0.51, 1.0)]],
    "n": [[(0.04, 0.46), (0.04, 1.0)], _arc(0.21, 0.64, 0.17, 0.17, 180, 360) + [(0.38, 1.0)]],
    "o": [_O],
    "p": [[(0.04, 0.46), (0.04, 1.3)], _arc(0.22, 0.73, 0.18, 0.27, 180, 540)],
    "r": [[(0.04, 0.46), (0.04, 1.0)], _arc(0.2, 0.62, 0.16, 0.15, 190, 320)],
    "s": [_arc(0.2, 0.6, 0.15, 0.13, -20, -270) + _arc(0.2, 0.86, 0.17, 0.14, -90, 160)],
    "t": [[(0.14, 0.12), (0.14, 0.94), (0.3, 1.0)], [(0.02, 0.46), (0.32, 0.46)]],
    "u": [[(0.04, 0.46)] + _arc(0.21, 0.82, 0.17, 0.18, 180, 0) + [(0.38, 0.46), (0.38, 1.0)]],
    "v": [[(0.02, 0.46), (0.2, 1.0), (0.38, 0.46)]],
    "w": [[(0.02, 0.46), (0.14, 1.0), (0.27, 0.6), (0.4, 1.0), (0.52, 0.46)]],
    "x": [[(0.03, 0.46), (0.38, 1.0)], [(0.38, 0.46), (0.03, 1.0)]],
}


# ── 계기 값·시각에 쓰는 기호 (tasks/0005 4.6): 소수점, 콜론, 물결표, 붙임표 ───────────────────
# 점은 작은 고리로 그린다 (굵기만큼 뭉쳐 점이 된다). 글자 상자 안의 높이(y)가 숫자와 다르므로 놓을 때 glyph_offset 만큼 내린다
def _dot(cx: float, cy: float, r: float = 0.035) -> Stroke:
    return _arc(cx, cy, r, r, 0, 360, 8)


PUNCT: dict[str, list[Stroke]] = {
    ".": [_dot(0.1, 0.94)],
    ":": [_dot(0.1, 0.34), _dot(0.1, 0.9)],
    "~": [[(0.0, 0.6), (0.08, 0.5), (0.18, 0.47), (0.3, 0.56), (0.42, 0.62), (0.52, 0.58), (0.6, 0.48)]],
    "-": [[(0.02, 0.56), (0.42, 0.54)]],
}


def glyph_offset(ch: str) -> float:
    """글자의 획이 차지하는 높이의 가운데 − 0.5 (글자 높이 대비). 숫자는 0 에 가깝다. 기호를 숫자 줄에 맞춰 놓을 때 쓴다
    (draw_glyph 의 그림은 획의 둘레로 잘리므로 가운데에 놓으면 소수점이 공중에 뜬다)."""
    if ch not in PUNCT:
        return 0.0
    ys = [p[1] for st in PUNCT[ch] for p in st]
    return (min(ys) + max(ys)) / 2 - 0.5


def strokes_for(ch: str, style: dict) -> list[Stroke]:
    """글자의 획. 숫자는 쓰는 사람의 꼴(variants)로, 기호(. : ~ -), 모르는 글자는 작은 고리."""
    if ch in DIGITS:
        forms = DIGITS[ch]
        return forms[style.get("variants", {}).get(ch, 0) % len(forms)]
    if ch in PUNCT:
        return PUNCT[ch]
    return LETTERS.get(ch.lower(), [_O])


def draw_glyph(ch: str, height_px: float, style: dict, rng: np.random.Generator) -> np.ndarray:
    """한 글자를 그려 잉크 마스크(0–1, float32)로. 글자 높이 height_px. 점마다 조금 흔들고(손떨림), 글자마다
    회전·크기·기울임을 다르게 한다. 둘레에 여유를 두어 뒤에서 비틀어도(elastic) 잘리지 않게."""
    return _render(strokes_for(ch, style), height_px, style, rng)


def draw_word(word: str, height_px: float, style: dict, rng: np.random.Generator) -> np.ndarray:
    """영문 소문자 낱말을 이어 쓴 꼴로 (글자 사이를 기준선의 짧은 획으로 잇는다). 메모 — 낱자가 숫자처럼 떨어져 보이지 않게."""
    strokes: list[Stroke] = []
    x = 0.0
    prev_end = None
    for ch in word:
        letter = LETTERS.get(ch.lower(), [_O])
        xs = [p[0] for s in letter for p in s]
        lo, hi = min(xs), max(xs)
        shifted = [[(px - lo + x, py) for px, py in s] for s in letter]
        if prev_end is not None:
            strokes.append([(prev_end, 0.97), (x + 0.02, 0.9)])
        strokes += shifted
        prev_end = x + (hi - lo)
        x += (hi - lo) + 0.1
    return _render(strokes, height_px, style, rng)


# 거친 손글씨 (synth --rough, tasks/0009 4.7 나): 떨림을 더하고, 획마다 굵기가 다르고, 가끔 연한 잉크와 번짐. 자기 난수로만 —
# 켜지 않으면 그리는 것이 바이트까지 그대로이고, 켜도 rng 의 흐름은 그대로다 (같은 글자를 같은 자리에 같은 모양으로 쓰고 거칠게만 한다)
ROUGH_TREMOR = 0.018              # 떨림: 점마다 글자 높이 대비 표준편차 (손떨림 wobble 0.012–0.027 위에 더한다)
ROUGH_SLANT = 0.2                 # 기울기: 글자마다 더하는 기울임 (±, 기준선에서의 높이 대비)
ROUGH_THICK = (-1, 3)             # 획마다 굵기에 더하는 화소 (정수, 끝은 빼고)
ROUGH_LIGHT = (0.3, 0.45, 0.8)    # 연한 잉크: 글자의 이 비율이 잉크 농도 0.45–0.8 배
ROUGH_BLOT = 0.04                 # 번짐: 글자의 이 비율에 획 위의 한 점이 굵기의 1–2 배 원으로
_ROUGH: list = []


@contextlib.contextmanager
def roughened(rng: np.random.Generator):
    """이 안에서 그리는 손글씨는 거칠다 (rng: 거칠기만의 난수)."""
    _ROUGH.append(rng)
    try:
        yield
    finally:
        _ROUGH.pop()


def _render(strokes: list[Stroke], height_px: float, style: dict, rng: np.random.Generator) -> np.ndarray:
    shear = style["slant"] + float(rng.uniform(-0.12, 0.12))
    ang = math.radians(float(rng.uniform(-10, 10)))
    sx, sy = float(rng.uniform(0.8, 1.2)), float(rng.uniform(0.88, 1.12))
    wobble = 0.012 + 0.015 * float(rng.random())                     # 손떨림: 글자 높이 대비 (크면 8·2 가 일그러져 사람도 못 읽는다)
    thick = max(1, int(round(style["thick"] * height_px / 22.0 * rng.uniform(0.8, 1.25))))
    pts_all = []
    for s in strokes:
        p = np.array(s, np.float64)
        p = p + rng.normal(0, wobble, (1, 2)) * 0.5 + rng.normal(0, wobble, p.shape) * 0.35
        x, y = p[:, 0] * sx, (p[:, 1] - 1.0) * sy                     # 기준선(y=1)을 원점으로
        x = x + shear * y                                             # 기울임 (예전 글꼴 변형과 같은 부호: 음수면 위가 오른쪽)
        xr = x * math.cos(ang) - y * math.sin(ang)
        yr = x * math.sin(ang) + y * math.cos(ang)
        pts_all.append(np.stack([xr, yr], 1) * height_px)
    rr = _ROUGH[-1] if _ROUGH else None
    if rr is not None:
        extra = float(rr.uniform(-ROUGH_SLANT, ROUGH_SLANT))
        pts_all = [np.stack([q[:, 0] + extra * q[:, 1], q[:, 1]], 1) + rr.normal(0, height_px * ROUGH_TREMOR, q.shape)
                   for q in pts_all]
    allp = np.concatenate(pts_all)
    m = int(height_px * 0.6) + thick * 2
    x0, y0 = allp[:, 0].min(), allp[:, 1].min()
    w = int(math.ceil(allp[:, 0].max() - x0)) + 2 * m + 1
    h = int(math.ceil(allp[:, 1].max() - y0)) + 2 * m + 1
    g8 = np.zeros((h, w), np.uint8)
    shift = 4                                                         # 1/16 화소 정밀도로 그린다
    polys = [np.round((q - [x0 - m, y0 - m]) * (1 << shift)).astype(np.int32) for q in pts_all]
    if rr is None:
        cv2.polylines(g8, polys, False, 255, thick, cv2.LINE_AA, shift)
        return g8.astype(np.float32) / 255.0
    for q in polys:
        cv2.polylines(g8, [q], False, 255, max(1, thick + int(rr.integers(*ROUGH_THICK))), cv2.LINE_AA, shift)
    if rr.random() < ROUGH_BLOT:
        q = polys[int(rr.integers(len(polys)))]
        px, py = (q[int(rr.integers(len(q)))] >> shift).tolist()
        cv2.circle(g8, (int(px), int(py)), max(2, int(round(thick * float(rr.uniform(1.0, 2.0))))), 255, -1, cv2.LINE_AA)
    out = g8.astype(np.float32) / 255.0
    if rr.random() < ROUGH_LIGHT[0]:
        out *= float(rr.uniform(*ROUGH_LIGHT[1:]))
    return out


def random_variants(rng: np.random.Generator) -> dict[str, int]:
    """쓰는 사람 한 명의 숫자 꼴."""
    return {d: int(rng.integers(len(forms))) for d, forms in DIGITS.items()}
