"""어려운 합성 셀: 숫자 칸 하나의 크롭을 규격대로 만든다 (tasks/0003 단계 3).

지금의 합성 양식은 칸이 크고 숫자가 한가운데에 깨끗하게 있다. 실제 운반 칸은 낮고 넓으며(200 dpi 에서 높이 28–30 px,
폭 약 100 px) 글씨가 칸보다 커서 위아래 괘선을 넘고, 그래서 이웃 칸의 글씨가 이 칸으로 넘어온다. 값이 있다고 판정된
칸의 약 5분의 1 은 이 칸의 숫자가 아니었다(X 표, 덧칠해 지운 것, 윗칸 숫자의 아랫부분, 칸 위에 걸쳐 쓴 메모).

이 생성기는 그런 칸을 정답과 함께 만든다. 한 장에 들어가는 것:
  · 가운데 칸의 값 (한두 자리, 칸보다 클 수 있다) — 없을 수도 있다
  · 위·아래·옆 칸의 숫자가 괘선을 넘어 들어온 것
  · 괘선 (조금 어긋나고 기울어진)
  · X 표, 덧칠해 지운 숫자, 칸 위에 걸쳐 쓴 메모
  · 스캔 효과 (흐림, 잡음, 종이색, 연한 잉크, JPEG)
정답은 숫자열(앞의 0 없음) 또는 빈 칸("")이다. 칸의 크기(템플릿 px)와 크롭 규격(CropSpec: 해상도·배율·여유)은
인자로 받는다 — 실제 크롭의 규격에 맞춰 만들 수 있게. 칸은 doc_field 의 bbox 와 같은 뜻이다: 괘선으로 둘러싼 칸에서
안쪽 여백(inset, 템플릿 기본 4 px)을 뺀 상자. 괘선은 그 바깥 inset 자리에 그린다. 같은 씨앗이면 같은 셀이 화소까지 같다.

글자는 OpenCV 내장 글꼴을 한 글자씩 비틀어 그린다 (글꼴 파일·외부 데이터에 의존하지 않는다). 그래서 이 셀로 잴 수 있는
것은 학습·추론 경로가 맞는지이지 손글씨 인식률이 아니다 (CLAUDE.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from ..imaging.cropspec import CropSpec

KINDS = ("value", "x", "scribble", "note", "spill", "blank")
FONTS = [cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX, cv2.FONT_HERSHEY_COMPLEX, cv2.FONT_HERSHEY_TRIPLEX,
         cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, cv2.FONT_HERSHEY_SCRIPT_COMPLEX, cv2.FONT_HERSHEY_PLAIN]
NOTE_WORDS = ["stop", "rain", "repair", "closed", "blast", "x", "move", "check", "late", "pump", "wait", "full"]


@dataclass
class CellParams:
    """만들 셀의 모양과 구성. 비율은 전체 셀에 대한 몫이고 make_cells 가 정확히 그 개수만큼 만든다."""

    cell_w: int = 92                       # 칸(doc_field bbox) 폭 (템플릿 px, 200 dpi) — 실제 운반 칸은 괘선 사이 약 100 px
    cell_h: int = 21                       # 칸 높이 — 괘선 사이 28–30 px 에서 inset 을 뺀 것
    inset: int = 4                         # 괘선과 bbox 사이 (forms/template.Template.cells 의 inset)
    value_h: tuple[float, float] = (0.6, 1.45)    # 가운데 값의 글자 높이 / 괘선 사이 높이. 1 을 넘으면 괘선을 넘는다
    spec: CropSpec = field(default_factory=lambda: CropSpec("source", 1.5, None))
    source_dpi_ratio: float = 1.5          # 원본 해상도 / 템플릿 해상도 (300 / 200)
    p_value: float = 0.55                  # 가운데 칸에 값이 있는 몫
    p_x: float = 0.12                      # X 표 (빈 칸)
    p_scribble: float = 0.06               # 덧칠해 지운 숫자 (빈 칸)
    p_note: float = 0.07                   # 칸 위에 걸쳐 쓴 메모 (빈 칸)
    p_blank: float = 0.05                  # 아무것도 없는 칸 (빈 칸) — 나머지 빈 칸은 이웃 글씨만 넘어온 것(spill)
    p_neighbor: float = 0.55               # 이웃 칸 글씨가 넘어온 셀의 몫 (spill 칸은 언제나 포함)
    max_value: int = 99

    def quotas(self, n: int) -> dict[str, int]:
        fr = {"value": self.p_value, "x": self.p_x, "scribble": self.p_scribble, "note": self.p_note, "blank": self.p_blank}
        if any(v < 0 for v in fr.values()) or sum(fr.values()) > 1 + 1e-9:
            raise ValueError(f"비율이 잘못되었습니다: {fr}")
        q = {k: int(round(n * v)) for k, v in fr.items()}
        q["spill"] = n - sum(q.values())
        if q["spill"] < 0:                                   # 반올림으로 넘친 만큼 value 에서 뺀다
            q["value"] += q["spill"]
            q["spill"] = 0
        return q

    def out_size(self) -> tuple[int, int]:
        """만든 셀의 (폭, 높이) — imaging/cropspec 이 같은 칸을 뜬 크기와 같다."""
        p = self.spec.pad_for((0, 0, self.cell_w, self.cell_h))
        s = float(self.spec.scale)
        return max(1, round((self.cell_w + 2 * p) * s)), max(1, round((self.cell_h + 2 * p) * s))


@dataclass
class SynthCell:
    image: np.ndarray                      # 회색조 uint8, CellParams.out_size() 크기
    text: str                              # 정답: 숫자열 또는 "" (빈 칸)
    kind: str                              # value | x | scribble | note | spill | blank
    neighbors: bool                        # 이웃 칸 글씨가 그려졌는가


def make_cells(n: int, seed: int = 0, params: CellParams | None = None) -> list[SynthCell]:
    """셀 n 개. 구성(값 있음·X 표·… ·이웃 글씨 있음)은 params 의 비율대로 정확한 개수로 섞는다.
    셀 i 는 (seed, i) 로만 정해진다 — 같은 씨앗이면 같은 셀. 여러 프로세스로 나눠 만들 때는 plan_cells 와 make_cell."""
    params = params or CellParams()
    return [make_cell(np.random.default_rng([seed, i]), params, k, nb) for i, (k, nb) in enumerate(plan_cells(n, seed, params))]


def plan_cells(n: int, seed: int, params: CellParams) -> list[tuple[str, bool]]:
    """셀 i 의 (종류, 이웃 글씨 여부). 셀 i 의 그림은 make_cell(default_rng([seed, i]), params, 종류, 여부)."""
    q = params.quotas(n)
    plan_rng = np.random.default_rng([seed, 1_000_003])
    kinds = [k for k in KINDS for _ in range(q[k])]
    plan_rng.shuffle(kinds)
    n_nb = int(round(n * params.p_neighbor))
    spill_idx = [i for i, k in enumerate(kinds) if k == "spill"]
    if n_nb < len(spill_idx):
        raise ValueError(f"p_neighbor({params.p_neighbor}) 는 spill 칸의 몫 이상이어야 합니다")
    others = [i for i, k in enumerate(kinds) if k not in ("spill", "blank")]
    plan_rng.shuffle(others)
    nb = set(spill_idx) | set(others[: n_nb - len(spill_idx)])
    if len(nb) < n_nb:                                       # blank 에도 이웃 글씨가 넘어올 수 있다
        blanks = [i for i, k in enumerate(kinds) if k == "blank"]
        nb |= set(blanks[: n_nb - len(nb)])
    return [(kinds[i], i in nb) for i in range(n)]


def random_value(rng: np.random.Generator, max_value: int = 99) -> str:
    """운반 횟수: 대부분 1–16, 두 자리가 드물지 않다. 학습에 없던 조합도 나오도록 20–99 를 조금 섞는다."""
    u = rng.random()
    if u < 0.50:
        v = int(rng.integers(1, 10))
    elif u < 0.85:
        v = int(rng.integers(10, 20))
    else:
        v = int(rng.integers(20, max(21, max_value + 1)))
    return str(min(v, max_value))


def make_cell(rng: np.random.Generator, params: CellParams, kind: str, neighbors: bool) -> SynthCell:
    if kind not in KINDS:
        raise ValueError(f"kind 는 {KINDS} 중 하나: {kind}")
    spec = params.spec
    native = params.source_dpi_ratio if spec.res == "source" else 1.0      # 그리는 해상도 (템플릿 px 당 화소)
    sup = 2.0                                                              # 계단을 줄이려고 두 배로 그린 뒤 줄인다
    r = native * sup
    cw, ch = params.cell_w, params.cell_h
    ins = params.inset
    gh = ch + 2 * ins                                                      # 괘선 사이 높이 (글자 크기의 기준)
    pad = spec.pad_for((0, 0, cw, ch))
    # 캔버스 = 크롭 영역(가운데 칸 + 여유). 템플릿 좌표의 원점은 가운데 칸의 왼쪽 위. 밖으로 나가는 획은 잘린다
    ox, oy = -pad, -pad
    W, H = int(round((cw + 2 * pad) * r)), int(round((ch + 2 * pad) * r))
    ink = np.zeros((H, W), np.float32)                                     # 0 = 종이, 1 = 진한 잉크

    def X(v: float) -> float:                                              # 템플릿 x → 캔버스
        return (v - ox) * r

    def Y(v: float) -> float:
        return (v - oy) * r

    def P(x: float, y: float) -> tuple[int, int]:
        return int(round(X(x))), int(round(Y(y)))

    jit = lambda s: rng.uniform(-s, s)                                     # noqa: E731
    # 괘선: 정합 뒤에도 몇 px 어긋나고 기울어진다
    line_w = max(1, int(round(rng.uniform(0.8, 1.6) * r)))
    for y in (-ins, ch + ins):
        cv2.line(ink, P(-pad - 5, y + jit(1.2)), P(cw + pad + 5, y + jit(1.2)), float(rng.uniform(0.75, 1.0)), line_w,
                 cv2.LINE_AA)
    for x in (-ins, cw + ins):
        cv2.line(ink, P(x + jit(1.2), -pad - 5), P(x + jit(1.2), ch + pad + 5), float(rng.uniform(0.75, 1.0)), line_w,
                 cv2.LINE_AA)
    style = _style(rng)

    # 이웃 칸의 숫자: 칸보다 크게 써서 괘선을 넘어 들어온다. 넘어오는 것은 꼬리 쪽이다 (괘선 사이 높이의 35 % 까지)
    if neighbors:
        sides = ["up", "down", "left", "right"]
        rng.shuffle(sides)
        for side in sides[: int(rng.integers(1, 4))]:
            text = random_value(rng, params.max_value)
            h = gh * rng.uniform(1.0, 1.5)
            depth = gh * rng.uniform(0.05, 0.35)                           # 괘선에서 가운데 칸 안으로 들어오는 깊이
            if side == "up":
                cx, cy = cw * 0.5 + jit(cw * 0.3), -ins + depth - h / 2
            elif side == "down":
                cx, cy = cw * 0.5 + jit(cw * 0.3), ch + ins - depth + h / 2
            elif side == "left":
                cx, cy = -cw * 0.5 + rng.uniform(0.05, 0.3) * cw, ch * 0.5 + jit(gh * 0.3)
            else:
                cx, cy = cw * 1.5 - rng.uniform(0.05, 0.3) * cw, ch * 0.5 + jit(gh * 0.3)
            _draw_number(ink, text, X(cx), Y(cy), h * r, rng, _style(rng))

    text = ""
    if kind in ("value", "scribble"):
        num = random_value(rng, params.max_value)
        h = gh * rng.uniform(*params.value_h)                              # 칸보다 클 수 있다 → 괘선을 넘는다
        cx, cy = cw * 0.5 + jit(cw * 0.18), ch * 0.5 + jit(gh * 0.12)
        _draw_number(ink, num, X(cx), Y(cy), h * r, rng, style)
        if kind == "value":
            text = num
        else:
            _scribble(ink, X(cx), Y(cy), h * r, rng, style)
    elif kind == "x":
        s = gh * rng.uniform(0.6, 1.3)
        a = s * rng.uniform(0.6, 1.4)
        cx, cy = cw * 0.5 + jit(cw * 0.2), ch * 0.5 + jit(gh * 0.1)
        th = max(1, int(round(style["thick"] * r)))
        c = style["ink"]
        cv2.line(ink, P(cx - a / 2 + jit(2), cy - s / 2 + jit(2)), P(cx + a / 2 + jit(2), cy + s / 2 + jit(2)), c, th,
                 cv2.LINE_AA)
        cv2.line(ink, P(cx + a / 2 + jit(2), cy - s / 2 + jit(2)), P(cx - a / 2 + jit(2), cy + s / 2 + jit(2)), c, th,
                 cv2.LINE_AA)
    elif kind == "note":
        words = " ".join(str(rng.choice(NOTE_WORDS)) for _ in range(int(rng.integers(1, 3))))
        h = gh * rng.uniform(0.6, 1.1)
        x_start = rng.uniform(-0.6, 0.2) * cw
        cy = -ins + rng.uniform(-0.15, 0.65) * gh                          # 칸 위에 걸치거나 칸을 지나간다
        _draw_text_line(ink, words, X(x_start), Y(cy), h * r, rng, style)
    # spill: 가운데 칸에는 아무것도 쓰지 않는다 (이웃 글씨만). blank: 아무것도 없다

    img = _scan(ink, rng, style)                                           # 종이·잉크 → 회색조, 흐림·잡음
    # 원본 해상도로 줄이고(JPEG 도 거기서), 출력 배율로 — aligned 는 200 dpi 로 떨어뜨린 뒤 키운다(파이프라인과 같은 거친 정도)
    nat = cv2.resize(img, (max(1, round((cw + 2 * pad) * native)), max(1, round((ch + 2 * pad) * native))),
                     interpolation=cv2.INTER_AREA)
    nat = _jpeg(_grain(nat, rng), rng)
    out_w, out_h = params.out_size()
    if (nat.shape[1], nat.shape[0]) != (out_w, out_h):
        nat = cv2.resize(nat, (out_w, out_h), interpolation=cv2.INTER_CUBIC if out_w > nat.shape[1] else cv2.INTER_AREA)
    return SynthCell(nat, text, kind, bool(neighbors))


# ── 그리기 ─────────────────────────────────────────────────────────────────
def _style(rng: np.random.Generator) -> dict:
    """쓰는 사람마다 다른 버릇: 글꼴, 굵기, 기울기, 잉크 진하기."""
    return {"font": FONTS[int(rng.integers(len(FONTS)))], "italic": bool(rng.random() < 0.3),
            "thick": float(rng.uniform(0.9, 2.6)), "slant": float(rng.uniform(-0.35, 0.25)),
            "ink": float(rng.uniform(0.45, 1.0)), "gap": float(rng.uniform(-0.12, 0.25))}


def _glyph(ch: str, height_px: float, style: dict, rng: np.random.Generator) -> np.ndarray:
    """한 글자를 따로 그려 비튼다. 결과는 잉크 마스크(0–1)."""
    font = style["font"] | (cv2.FONT_ITALIC if style["italic"] else 0)
    base_h = cv2.getTextSize("8", font, 1.0, 1)[0][1]
    scale = max(0.2, height_px / max(1, base_h))
    thick = max(1, int(round(style["thick"] * height_px / 22.0 * rng.uniform(0.8, 1.25))))
    (tw, th), bl = cv2.getTextSize(ch, font, scale, thick)
    m = int(height_px * 0.6) + thick * 2
    g8 = np.zeros((th + bl + 2 * m, tw + 2 * m), np.uint8)               # putText 는 8비트 그림에만 그린다 (OpenCV 5)
    cv2.putText(g8, ch, (m, m + th), font, scale, 255, thick, cv2.LINE_AA)
    g = g8.astype(np.float32) / 255.0
    # 글자마다 기울기·회전·크기·찌그러짐
    hh, ww = g.shape
    ang = float(rng.uniform(-12, 12))
    sx, sy = float(rng.uniform(0.8, 1.2)), float(rng.uniform(0.85, 1.15))
    shear = style["slant"] + float(rng.uniform(-0.12, 0.12))
    a = cv2.getRotationMatrix2D((ww / 2, hh / 2), ang, 1.0)
    a = np.vstack([a, [0, 0, 1]]) @ np.array([[sx, shear, -shear * hh / 2 + (1 - sx) * ww / 2],
                                              [0, sy, (1 - sy) * hh / 2], [0, 0, 1]])
    g = cv2.warpAffine(g, a[:2], (ww, hh), flags=cv2.INTER_LINEAR, borderValue=0)
    g = _elastic(g, rng, alpha=height_px * 0.06, sigma=max(2.0, height_px * 0.12))
    return g


def _elastic(g: np.ndarray, rng: np.random.Generator, alpha: float, sigma: float) -> np.ndarray:
    """부드럽게 휘게 한다: 거친 격자(4×4)의 무작위 변위를 키워 쓴다 (전체 크기에서 흐리는 것보다 훨씬 빠르다)."""
    hh, ww = g.shape
    grid = max(3, int(round(max(hh, ww) / max(sigma * 2.5, 1.0))))
    dx = cv2.resize(rng.uniform(-1, 1, (grid, grid)).astype(np.float32), (ww, hh), interpolation=cv2.INTER_CUBIC) * alpha
    dy = cv2.resize(rng.uniform(-1, 1, (grid, grid)).astype(np.float32), (ww, hh), interpolation=cv2.INTER_CUBIC) * alpha
    xs, ys = np.meshgrid(np.arange(ww, dtype=np.float32), np.arange(hh, dtype=np.float32))
    return cv2.remap(g, xs + dx, ys + dy, cv2.INTER_LINEAR, borderValue=0)


def _paste(ink: np.ndarray, g: np.ndarray, cx: float, cy: float, strength: float) -> None:
    """g 의 가운데를 (cx, cy) 에 놓는다. 겹치는 잉크는 더 진한 쪽."""
    hh, ww = g.shape
    x0, y0 = int(round(cx - ww / 2)), int(round(cy - hh / 2))
    H, W = ink.shape
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(W, x0 + ww), min(H, y0 + hh)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    ink[sy0:sy1, sx0:sx1] = np.maximum(ink[sy0:sy1, sx0:sx1], g[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] * strength)


def _draw_number(ink: np.ndarray, text: str, cx: float, cy: float, height: float, rng, style: dict) -> None:
    glyphs = [_glyph(c, height * float(rng.uniform(0.9, 1.1)), style, rng) for c in text]
    widths = [_ink_width(g) for g in glyphs]
    gap = style["gap"] * height
    total = sum(widths) + gap * (len(glyphs) - 1)
    x = cx - total / 2
    for g, w in zip(glyphs, widths, strict=True):
        _paste(ink, g, x + w / 2, cy + rng.uniform(-0.08, 0.08) * height, style["ink"] * float(rng.uniform(0.85, 1.0)))
        x += w + gap


def _ink_width(g: np.ndarray) -> float:
    cols = np.where(g.max(axis=0) > 0.2)[0]
    return float(cols[-1] - cols[0] + 1) if len(cols) else g.shape[1] * 0.5


def _draw_text_line(ink: np.ndarray, words: str, x_start: float, cy: float, height: float, rng, style: dict) -> None:
    x = x_start
    for ch in words:
        if ch == " ":
            x += height * 0.45
            continue
        g = _glyph(ch, height * float(rng.uniform(0.8, 1.0)), style, rng)
        w = _ink_width(g)
        _paste(ink, g, x + w / 2, cy, style["ink"])
        x += w + height * 0.05


def _scribble(ink: np.ndarray, cx: float, cy: float, height: float, rng, style: dict) -> None:
    """덧칠해 지운 것: 숫자 위를 지그재그로 빽빽하게 그어 덮는다."""
    w = height * float(rng.uniform(0.8, 1.6))
    th = max(1, int(round(style["thick"] * height / 18.0)))
    n = int(rng.integers(5, 11))
    pts = []
    for i in range(n + 1):
        x = cx - w / 2 + w * i / n + rng.uniform(-2, 2)
        y = cy + (height * 0.45 if i % 2 else -height * 0.45) + rng.uniform(-3, 3)
        pts.append((int(round(x)), int(round(y))))
    cv2.polylines(ink, [np.array(pts, np.int32)], False, style["ink"], th, cv2.LINE_AA)
    if rng.random() < 0.5:                                  # 가로로도 한 번 더
        cv2.line(ink, (int(cx - w / 2), int(cy)), (int(cx + w / 2), int(cy + rng.uniform(-3, 3))), style["ink"], th,
                 cv2.LINE_AA)


def _scan(ink: np.ndarray, rng, style: dict) -> np.ndarray:
    """잉크 → 회색조: 종이색, 잉크 진하기, 종이의 얼룩(거친 격자를 키운 것), 흐림. 화소 잡음은 줄인 뒤에 _grain 으로."""
    paper = float(rng.uniform(200, 250))
    dark = float(rng.uniform(10, 80))
    img = paper - np.clip(ink, 0, 1) * (paper - dark)
    blotch = cv2.resize(rng.standard_normal((6, 12), dtype=np.float32), (img.shape[1], img.shape[0]),
                        interpolation=cv2.INTER_CUBIC)
    img = img + blotch * float(rng.uniform(0, 6))
    return cv2.GaussianBlur(img, (0, 0), float(rng.uniform(0.6, 1.6)))


def _grain(img: np.ndarray, rng) -> np.ndarray:
    noisy = img + rng.standard_normal(img.shape, dtype=np.float32) * float(rng.uniform(1, 6))
    return np.clip(noisy, 0, 255).astype(np.uint8)


def _jpeg(img: np.ndarray, rng) -> np.ndarray:
    if rng.random() < 0.5:
        return img
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(55, 95))])
    return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE) if ok else img
