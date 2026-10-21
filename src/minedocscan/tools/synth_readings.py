"""소수·시각 칸의 합성 크롭 (tasks/0011 4.5) — 소수·시각 모델(recognize/readings)의 학습·시험이 쓰는 생성기.

tools/synth_cells(숫자 칸)와 같은 자리, 같은 획(tools/handfont)과 같은 스캔 효과(synth_cells 의 _grain·_jpeg, _scan 은 흐림의 폭만
원본 해상도에 맞춘 사본). 칸 하나의 크롭을 규격(CropSpec)대로 정답과 함께 만든다 — 크롭 안의 칸 자리(cell)도 같이 (글씨 띠 —
recognize/readings/model.band 가 쓴다).
같은 씨앗이면 화소까지 같고 난수는 따로 쓴다: 기존 합성(synth 의 모든 선택, synth_cells)의 바이트는 그대로다 (handfont 의 표를 고치지
않는다 — 흘려 쓴 00, 사선 하나의 1 은 이 모듈의 획이다).

실제 칸에서 본 것 (1절 나 — 실제 가동 일보 40쪽):
  · 값: 계기 값(세–다섯 자리 + 소수 한 자리, 가끔 소수 없이), 시각(콜론, 점, 붙여 쓴 0800 — 붙여 쓴 것은 time·time_range 칸에서만:
    reading 칸의 0800 은 계기 값 800 이다), 시각 범위(정규형, 8-12, 인쇄된 ~ 양옆에 쓴 8·12, 섞인 것)
  · 꼴: 흘려 쓴 00(고리 둘), 사선 하나의 1, 칸보다 큰 글씨(괘선 사이 높이의 0.6–1.2), 칸 폭을 거의 다 쓰는 값(0.93–1.01), 덧쓴 숫자,
    윗줄·아랫줄과 옆 칸(같은 줄의 다른 값)에서 넘어온 글씨, 키 큰 칸(근무 시각 99 px 같은)의 작은 글씨(칸 높이의 0.2–0.3)
  · 인쇄: 계기 칸 왼쪽의 인쇄된 이름표(합성 글자 몇 개와 ":"), 근무 시각 칸 가운데의 인쇄된 "~" (synth_usage._tilde 와 같은 선).
    인쇄만 있는 칸의 정답은 빈 칸이다
  · 빈 칸의 갈래: X 표, 덧칠, 칸 위에 걸친 메모, 이웃 글씨만, 인쇄만, 아무것도 없음 (synth_cells.KINDS 와 같은 뜻)
한글 인쇄 글자(실제 계기 칸의 "시작:")는 그리지 않는다 (글꼴이 없다) — 실제 크롭으로 학습할 때 들어온다.

정답(text)은 종이에 적힌 글자(인쇄된 "~" 를 사이에 둔 값이면 그것까지 — "8~12"), 빈 칸은 "", 덧쓴 숫자는 거절 "?".
value 는 사람이 넣을 정규화한 값 (점으로 쓴 시각은 시각 — 1절 라): text 는 늘 formats.spellings(fmt, value) 안에 있다.
이 칸으로 잴 수 있는 것은 학습·추론 경로가 맞는지이지 손글씨 인식률이 아니다 (CLAUDE.md).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from ..forms.formats import normalize
from ..imaging.cropspec import CropSpec
from . import handfont, synth_cells

FORMATS = ("reading", "time_range", "decimal", "time")
# 기본 비율 (4.5): 형식마다 칸의 몫, reading 값의 갈래, time_range·time 값의 갈래
FORMAT_SHARES = {"reading": 0.5, "time_range": 0.3, "decimal": 0.1, "time": 0.1}
MODES = {
    "reading": (("meter", 0.75), ("colon", 0.15), ("dotted", 0.10)),
    "time_range": (("normal", 0.35), ("dash", 0.2), ("printed", 0.3), ("mixed", 0.15)),
    "time": (("colon", 0.4), ("dot", 0.2), ("joined", 0.25), ("hour", 0.15)),
    "decimal": (("decimal", 1.0),),
}
KINDS = ("value", "overwrite", "x", "scribble", "note", "spill", "printed", "blank")
EMPTY_KINDS = ("x", "scribble", "note", "spill", "printed", "blank")
LABEL_WORDS = ("st", "end", "tot", "hrs", "meter", "start", "fin")      # 계기 칸의 인쇄된 이름표 (합성 글자 — 실제는 "시작:" 같은 한글)
LABEL_MIN_W = 250                  # 인쇄된 이름표를 그리는 칸의 최소 폭 (템플릿 px)
TALL = 90                          # 이 높이(템플릿 px) 이상인 칸은 "키 큰 칸" — 글씨가 칸 높이의 0.2–0.3 (실제 근무 시각 99·100 px.
                                   # V2 의 유류일지·환경일지 칸(82 px)은 글씨가 괘선 사이 높이의 0.5–0.65 라 키 큰 칸이 아니다)
SUPERSAMPLE = 1.0                  # 원본 해상도로 그린다 — 2 배로 그리면 큰 글씨(괘선 사이 82 px 칸)의 칸 하나가 65 ms 라 학습 시간의
                                   # 8분의 1 을 칸 만들기에 썼다 (흐림은 _scan 이 맞춘다)
TILDE_W, TILDE_A, TILDE_T = 32, 5, 3                                     # 인쇄된 "~" — synth_usage 와 같은 선 (폭, 진폭, 굵기, 템플릿 px)


@dataclass
class ReadingParams:
    """만들 칸의 모양과 구성. 비율은 그 형식의 칸 전체에 대한 몫이고 plan_readings 가 정확히 그 개수만큼 만든다."""

    fmt: str = "reading"
    cell_w: int = 362                      # 칸(doc_field bbox) 폭 (템플릿 px, 200 dpi) — 합성 가동 일보의 계기 칸
    cell_h: int = 38
    inset: int = 4
    spec: CropSpec = field(default_factory=lambda: CropSpec("source", 1.5, None))
    source_dpi_ratio: float = 1.5
    value_h: tuple[float, float] = (0.6, 1.2)      # 쓴 글씨(잉크의 세로 범위 — _sized_ink) / 괘선 사이 높이 (1 을 넘으면 괘선을 넘는다, 4.5).
                                                   # V2 의 높은 칸(82 px)의 글씨(꼴 높이 0.5–0.65 → 잉크 0.65–0.85)도 이 안이다
    tall_h: tuple[float, float] = (0.2, 0.3)       # 키 큰 칸: 쓴 글씨의 높이 / 칸 높이
    p_value: float = 0.62
    p_overwrite: float = 0.03              # 덧쓴 숫자 → 거절 "?"
    p_x: float = 0.06
    p_scribble: float = 0.04
    p_note: float = 0.05
    p_printed: float = 0.08                # 인쇄만 있는 칸 (reading 의 이름표, time_range 의 ~) — 인쇄가 없는 형식은 blank 로
    p_blank: float = 0.04                  # 나머지 빈 칸은 이웃 글씨만 넘어온 것(spill)
    p_neighbor: float = 0.5
    p_label: float = 0.5                   # 값 칸·빈 칸에 인쇄(이름표·~)가 있는 몫 (reading·time_range)
    p_wide: float = 0.5                    # 칸 폭을 거의 다 쓰는 값 — 실제 계기 칸의 글씨 폭 / 칸 폭은 중앙 0.93 (1절 나)

    def printed_format(self) -> bool:
        return self.fmt in ("reading", "time_range")

    def quotas(self, n: int) -> dict[str, int]:
        fr = {"value": self.p_value, "overwrite": self.p_overwrite, "x": self.p_x, "scribble": self.p_scribble,
              "note": self.p_note, "printed": self.p_printed if self.printed_format() else 0.0,
              "blank": self.p_blank + (0.0 if self.printed_format() else self.p_printed)}
        if any(v < 0 for v in fr.values()) or sum(fr.values()) > 1 + 1e-9:
            raise ValueError(f"비율이 잘못되었습니다: {fr}")
        q = _exact(n, fr)
        q["spill"] = n - sum(q.values())
        return q

    def cell_in_crop(self) -> tuple[float, float, float, float]:
        """크롭 안의 칸 자리 (크롭 화소) — imaging/cropspec.crop_box 와 같은 기하 (여유 p, 배율 s)."""
        p, s = self.spec.pad_for((0, 0, self.cell_w, self.cell_h)), float(self.spec.scale)
        return p * s, p * s, (p + self.cell_w) * s, (p + self.cell_h) * s

    def out_size(self) -> tuple[int, int]:
        p, s = self.spec.pad_for((0, 0, self.cell_w, self.cell_h)), float(self.spec.scale)
        return max(1, round((self.cell_w + 2 * p) * s)), max(1, round((self.cell_h + 2 * p) * s))


@dataclass
class ReadingCell:
    image: np.ndarray                      # 회색조 uint8, ReadingParams.out_size() 크기
    text: str                              # 정답: 적힌 글자 | "" (빈 칸) | "?" (덧쓴 숫자)
    value: str                             # 사람이 넣을 정규화한 값 ("" — 빈 칸·거절)
    fmt: str
    kind: str                              # KINDS
    mode: str                              # 값의 갈래 (MODES) — 빈 칸은 ""
    label: bool                            # 인쇄(이름표·~)가 있는가
    neighbors: bool
    cell: tuple[float, float, float, float]    # 크롭 안의 칸 자리 (크롭 화소)
    ink_box: tuple[float, float, float, float] | None = None   # 이 칸의 글씨(값)가 차지한 자리 (크롭 화소) — 띠의 시험
    other_box: tuple[float, float, float, float] | None = None  # 키 큰 칸의 윗줄 글씨 자리 (크롭 화소) — 띠의 시험
    value_mask: np.ndarray | None = None   # masks=True: 이 칸의 글씨(값)의 잉크 (크롭 크기, bool) — 띠의 시험
    other_mask: np.ndarray | None = None   # masks=True: 키 큰 칸의 윗줄 글씨의 잉크 (크롭 크기, bool)


def _exact(n: int, shares: dict[str, float]) -> dict[str, int]:
    """n 을 몫대로 정확히 나눈다 (큰 나머지 순 — 합이 n 을 넘지 않는다, 몫의 합이 1 이면 n)."""
    raw = {k: n * v for k, v in shares.items()}
    q = {k: int(math.floor(v)) for k, v in raw.items()}
    rest = int(round(n * sum(shares.values()))) - sum(q.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - q[k]), list(raw).index(k)))[:max(0, rest)]:
        q[k] += 1
    return q


# ── 계획 ───────────────────────────────────────────────────────────────────
def plan_readings(n: int, seed: int, params: ReadingParams) -> list[tuple[str, bool, bool, str]]:
    """칸 i 의 (종류, 이웃 글씨, 인쇄, 값의 갈래). 칸 i 의 그림은 make_reading(default_rng([seed, i]), params, …)."""
    q = params.quotas(n)
    rng = np.random.default_rng([seed, 1_000_003])
    kinds = [k for k in KINDS for _ in range(q.get(k, 0))]
    rng.shuffle(kinds)
    values = [i for i, k in enumerate(kinds) if k == "value"]
    mq = _exact(len(values), dict(MODES[params.fmt]))
    modes_list = [m for m, c in mq.items() for _ in range(c)]
    rng.shuffle(modes_list)
    mode = dict(zip(values, modes_list, strict=True))
    n_nb = int(round(n * params.p_neighbor))
    spill = [i for i, k in enumerate(kinds) if k == "spill"]
    others = [i for i, k in enumerate(kinds) if k != "spill"]
    rng.shuffle(others)
    nb = set(spill) | set(others[: max(0, n_nb - len(spill))])
    out = []
    for i, k in enumerate(kinds):
        m = mode.get(i, "")
        label = params.printed_format() and (k == "printed" or m == "printed" or
                                              (k not in ("blank",) and bool(rng.random() < params.p_label)))
        out.append((k, i in nb, bool(label), m))
    return out


def plan_set(n: int, seed: int, geometry: dict[str, list[tuple[int, int]]], shares: dict[str, float] | None = None
             ) -> list[tuple[str, tuple[int, int], int]]:
    """여러 형식·칸 크기에 n 개를 나눈다: [(형식, (폭, 높이), 그 묶음의 칸 수)]. 형식의 몫(shares, 기본 FORMAT_SHARES)을 geometry 에
    있는 형식끼리 다시 맞추고, 형식 안에서는 칸 크기마다 고르게."""
    shares = {f: (shares or FORMAT_SHARES).get(f, 0.0) for f in geometry if geometry[f]}
    total = sum(shares.values()) or 1.0
    per = _exact(n, {f: v / total for f, v in shares.items()})
    out = []
    for fmt, k in per.items():
        sizes = list(geometry[fmt])
        for g, wh in enumerate(sizes):
            n_g = k // len(sizes) + (1 if g < k % len(sizes) else 0)
            if n_g:
                out.append((fmt, (int(wh[0]), int(wh[1])), n_g))
    return out


# ── 값 ────────────────────────────────────────────────────────────────────
def _hour(rng) -> int:
    return int(rng.integers(5, 24))


def _minute(rng) -> int:
    u = rng.random()
    return 0 if u < 0.6 else 30 if u < 0.8 else int(rng.choice([10, 15, 20, 40, 45, 50]))


def _time_text(rng, h: int, m: int, mode: str) -> str:
    """시각 하나의 적는 법 (formats.spellings 의 time 안에서)."""
    hh = f"{h:02d}" if rng.random() < 0.5 else str(h)
    if mode == "hour" and m == 0:
        return hh
    if mode == "dot":
        return f"{hh}.{m:02d}"
    if mode == "joined":
        return f"{hh}{m:02d}"
    return f"{hh}:{m:02d}"


def random_value(rng: np.random.Generator, fmt: str, mode: str) -> tuple[str, str]:
    """(적힌 글자, 정규화한 값). 인쇄된 ~ 를 사이에 둔 시각 범위("printed")는 양쪽을 "a~b" 로 — 그리는 쪽이 ~ 를 인쇄로 둔다."""
    if fmt == "decimal":
        whole = str(int(rng.integers(0, 1000)))
        frac = "".join(str(int(rng.integers(0, 10))) for _ in range(int(rng.integers(1, 3))))
        text = f"{whole}.{frac}"
        return text, normalize("decimal", text)
    if fmt == "reading":
        if mode == "meter":
            digits = int(rng.choice([3, 4, 4, 5, 5]))
            whole = str(int(rng.integers(10 ** (digits - 1), 10 ** digits)))
            text = whole if rng.random() < 0.1 else f"{whole}.{int(rng.integers(0, 10))}"
            return text, normalize("reading", text)
        h, m = _hour(rng), _minute(rng)
        hh = f"{h:02d}" if rng.random() < 0.6 else str(h)
        sep = ":" if mode == "colon" else "."
        return f"{hh}{sep}{m:02d}", f"{h:02d}:{m:02d}"
    if fmt == "time":
        h, m = _hour(rng), _minute(rng)
        if mode == "hour":
            m = 0
        return _time_text(rng, h, m, mode), f"{h:02d}:{m:02d}"
    # time_range
    h0 = int(rng.integers(5, 20))
    h1 = (h0 + int(rng.integers(2, 13))) % 24
    m0, m1 = _minute(rng), _minute(rng)
    if mode == "dash":
        m0 = m1 = 0
        a, b, sep = str(h0) if rng.random() < 0.7 else f"{h0:02d}", str(h1), "-"
    elif mode == "normal":
        a, b, sep = f"{h0:02d}:{m0:02d}", f"{h1:02d}:{m1:02d}", "~"
    else:                                   # mixed · printed: 양쪽이 서로 다른 적는 법
        sides = ("colon", "dot", "joined", "hour")
        a = _time_text(rng, h0, m0, str(rng.choice(sides)) if m0 == 0 else str(rng.choice(sides[:3])))
        b = _time_text(rng, h1, m1, str(rng.choice(sides)) if m1 == 0 else str(rng.choice(sides[:3])))
        sep = "~" if mode == "printed" or rng.random() < 0.5 else "-"
    return f"{a}{sep}{b}", f"{h0:02d}:{m0:02d}~{h1:02d}:{m1:02d}"


# ── 그리기 ─────────────────────────────────────────────────────────────────
SLASH_ONE = [[(0.42, 0.0), (0.14, 1.0)]]                                     # 사선 하나의 1 (1절 나 — 길게 긋는다)
LOOP_00 = [handfont._arc(0.22, 0.5, 0.2, 0.48, -60, 300) + handfont._arc(0.62, 0.5, 0.2, 0.46, 200, 520)]   # 흘려 쓴 00


def _elastic(g: np.ndarray, rng, alpha: float, sigma: float) -> np.ndarray:
    """synth_cells._elastic 과 같은 휘기 — 거친 격자의 변위를 선형 보간으로 키운다 (3차 보간이 칸 하나를 만드는 시간의 대부분이었다).
    synth_cells 의 것은 그대로 둔다 (그 칸의 바이트가 바뀌지 않게)."""
    hh, ww = g.shape
    grid = max(3, int(round(max(hh, ww) / max(sigma * 2.5, 1.0))))
    dx = cv2.resize(rng.uniform(-1, 1, (grid, grid)).astype(np.float32), (ww, hh), interpolation=cv2.INTER_LINEAR) * alpha
    dy = cv2.resize(rng.uniform(-1, 1, (grid, grid)).astype(np.float32), (ww, hh), interpolation=cv2.INTER_LINEAR) * alpha
    xs = np.arange(ww, dtype=np.float32)[None, :] + dx
    ys = np.arange(hh, dtype=np.float32)[:, None] + dy
    return cv2.remap(g, xs, ys, cv2.INTER_LINEAR, borderValue=0)


def _glyph(ch: str, h: float, style: dict, rng) -> np.ndarray:
    return _elastic(handfont.draw_glyph(ch, h, style, rng), rng, alpha=h * 0.06, sigma=max(2.0, h * 0.12))


def _glyphs(text: str, h: float, style: dict, rng) -> list[tuple[str, np.ndarray]]:
    """글자마다 (글자, 잉크). 쓰는 사람의 버릇(style["slash1"]·["loop00"])이면 1 을 사선으로, 00 을 고리 둘로 이어 쓴다."""
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "0" and text[i:i + 2] == "00" and style.get("loop00"):
            g = handfont._render(LOOP_00, h, style, rng)
            out.append(("00", _elastic(g, rng, alpha=h * 0.05, sigma=max(2.0, h * 0.12))))
            i += 2
            continue
        if ch == "1" and style.get("slash1"):
            g = handfont._render(SLASH_ONE, h * float(rng.uniform(1.0, 1.25)), style, rng)
            out.append(("1", _elastic(g, rng, alpha=h * 0.05, sigma=max(2.0, h * 0.12))))
        else:
            out.append((ch, _glyph(ch, h * (float(rng.uniform(0.92, 1.06)) if ch.isdigit() else 1.0), style, rng)))
        i += 1
    return out


def _extent(g: np.ndarray) -> float:
    """글자의 폭: 잉크 질량의 1 %–99 % 구간 (synth_usage._ink_extent 와 같은 생각 — 판마다의 안티에일리어싱 차이에 덜 흔들린다)."""
    col = g.sum(axis=0).astype(np.float64)
    if col.sum() <= 0:
        return g.shape[1] * 0.5
    c = np.cumsum(col) / col.sum()
    return float(np.interp(0.99, c, np.arange(len(c))) - np.interp(0.01, c, np.arange(len(c)))) + 1.0


def _text_ink(text: str, h: float, style: dict, rng) -> tuple[np.ndarray, float, float]:
    """숫자·기호 한 줄의 잉크 (0–1). 돌려주는 값: (그림, 글씨가 시작하는 x, 글씨 폭). 세로 가운데 = 그림 높이의 반."""
    glyphs = _glyphs(text, h, style, rng)
    widths = [_extent(g) for _c, g in glyphs]
    gap = h * float(rng.uniform(0.04, 0.14))
    total = sum(widths) + gap * (len(glyphs) - 1)
    m = h * 0.8
    ink = np.zeros((int(h * 2.6), int(total + 2 * m + 2)), np.float32)
    x, cy = m, ink.shape[0] / 2
    for (ch, g), w in zip(glyphs, widths, strict=True):
        dy = handfont.glyph_offset(ch[0]) * h if len(ch) == 1 else 0.0
        synth_cells._paste(ink, g, x + w / 2, cy + dy + float(rng.uniform(-0.05, 0.05)) * h,
                           style["ink"] * float(rng.uniform(0.9, 1.0)))
        x += w + gap
    return ink, m, total


def _sized_ink(text: str, h: float, style: dict, rng) -> tuple[np.ndarray, float, float]:
    """_text_ink 를 잉크의 세로 범위가 h 가 되게 (가로·세로 같이) 줄이거나 키운 것. h 는 획의 꼴 높이가 아니라 **쓴 글씨의 높이** —
    1절 나의 "글씨 높이"(정합 그림에서 잰 잉크의 세로 범위)와 같은 뜻이다. 꼴 높이로 그리면 글자마다의 크기·자리 흔들림과 휘기 때문에
    잉크가 h 의 1.3 배쯤 되어, 괘선 사이 높이의 1.2 배로 정한 가장 큰 글씨가 1.57 배까지 커졌다 (실제 계기 칸의 글씨는 칸 높이를 넘지 않았다)."""
    ink, m, total = _text_ink(text, h, style, rng)
    ys = np.nonzero((ink > 0.25).any(axis=1))[0]
    if not len(ys):
        return ink, m, total
    k = h / float(ys[-1] - ys[0] + 1)
    ink = cv2.resize(ink, (max(1, int(round(ink.shape[1] * k))), max(1, int(round(ink.shape[0] * k)))),
                     interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_LINEAR)
    return ink, m * k, total * k


def _put(canvas: np.ndarray, ink: np.ndarray, x_left: float, cy: float,
         also: np.ndarray | None = None) -> tuple[float, float, float, float] | None:
    """잉크 그림의 왼쪽 위를 (x_left, cy − 높이/2) 에 놓는다. 겹치면 진한 쪽. also: 같은 자리에 같이 그릴 캔버스 (시험의 마스크).
    돌려주는 값: 캔버스에서 잉크가 있는 상자."""
    hh, ww = ink.shape
    x0, y0 = int(round(x_left)), int(round(cy - hh / 2))
    H, W = canvas.shape
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(W, x0 + ww), min(H, y0 + hh)
    if sx1 <= sx0 or sy1 <= sy0:
        return None
    patch = ink[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0]
    canvas[sy0:sy1, sx0:sx1] = np.maximum(canvas[sy0:sy1, sx0:sx1], patch)
    if also is not None:
        also[sy0:sy1, sx0:sx1] = np.maximum(also[sy0:sy1, sx0:sx1], patch)
    ys, xs = np.nonzero(patch > 0.25)
    if not len(xs):
        return None
    return float(sx0 + xs.min()), float(sy0 + ys.min()), float(sx0 + xs.max() + 1), float(sy0 + ys.max() + 1)


def _write(canvas, text: str, h: float, cx: float, cy: float, rng, style, fit_w: float | None = None,
           max_w: float | None = None, also: np.ndarray | None = None, within: tuple[float, float] | None = None):
    """가운데 (cx, cy) 근처에 쓴다 (캔버스 화소). fit_w 를 주면 그 폭으로 가로를 맞춘다 (칸 폭을 거의 다 쓰는 값). max_w 보다 넓으면
    그 폭의 0.85–1.0 으로 줄인다 (가로·세로 같이 — 실제 값의 폭은 칸 폭의 1.01 배를 넘지 않았다, 1절 나). within=(왼쪽, 오른쪽)을 주면
    글씨가 그 안에 들도록 가로 자리를 당긴다 — 칸 폭만 한 값을 가운데에서 비켜 쓰면 옆 칸으로 넘어가는데, 실제 값은 칸을 넘지 않았다.
    잉크 상자를 돌려준다."""
    ink, m, total = _sized_ink(text, h, style, rng)
    if fit_w is not None and total > 0:
        k = fit_w / total
        ink = cv2.resize(ink, (max(1, int(round(ink.shape[1] * k))), ink.shape[0]), interpolation=cv2.INTER_LINEAR)
        m, total = m * k, fit_w
    elif max_w is not None and total > max_w > 0:              # 좁으면 먼저 가로로 좁혀 쓰고(0.6 배까지), 그래도 넓으면 작게 쓴다
        k = max_w * float(rng.uniform(0.85, 1.0)) / total
        ky = min(1.0, k / 0.6)
        ink = cv2.resize(ink, (max(1, int(round(ink.shape[1] * k))), max(1, int(round(ink.shape[0] * ky)))),
                         interpolation=cv2.INTER_AREA)
        m, total = m * k, total * k
    start = cx - total / 2
    if within is not None:
        lo, hi = within
        start = (lo + hi - total) / 2 if total >= hi - lo else min(max(start, lo), hi - total)
    return _put(canvas, ink, start - m, cy, also)


def _print_text(canvas, text: str, x: float, cy: float, h: float, thick: int, value: float) -> float:
    """인쇄된 글자 (흔들림 없이 — handfont 의 소문자·기호 획을 그대로). 돌려주는 값: 끝난 x."""
    for ch in text:
        strokes = handfont.PUNCT.get(ch) or handfont.LETTERS.get(ch, [])
        xs = [p[0] for s in strokes for p in s] or [0.0, 0.3]
        lo, hi = min(xs), max(xs)
        polys = [np.round(np.array([((px - lo) * h + x, (py - 0.5) * h + cy) for px, py in s]) * 16).astype(np.int32)
                 for s in strokes]
        if polys:
            cv2.polylines(canvas, polys, False, value, thick, cv2.LINE_AA, 4)
        x += (hi - lo) * h + h * 0.12
    return x


def _tilde(canvas, cx: float, cy: float, r: float) -> None:
    t = np.linspace(0.0, 2 * np.pi, 25)
    pts = np.stack([cx + (t / (2 * np.pi) - 0.5) * TILDE_W * r, cy - TILDE_A * r * np.sin(t)], 1)
    cv2.polylines(canvas, [np.round(pts * 16).astype(np.int32)], False, 0.92, max(1, int(round(TILDE_T * r))), cv2.LINE_AA, 4)


def _scan(ink: np.ndarray, rng, style: dict, sup: float) -> np.ndarray:
    """synth_cells._scan 과 같은 종이·잉크·얼룩·흐림 (같은 난수를 같은 순서로) — 흐림의 폭만 초표본 배율로 나눈다: 그 함수는 2 배로
    그린 캔버스에 쓰는 흐림이라, 원본 해상도(sup 1)로 그리는 이 모듈에서는 같은 겉보기가 되게 반으로."""
    paper = float(rng.uniform(200, 250))
    dark = float(rng.uniform(10, 80))
    img = paper - np.clip(ink, 0, 1) * (paper - dark)
    blotch = cv2.resize(rng.standard_normal((6, 12), dtype=np.float32), (img.shape[1], img.shape[0]),
                        interpolation=cv2.INTER_CUBIC)
    img = img + blotch * float(rng.uniform(0, 6))
    return cv2.GaussianBlur(img, (0, 0), float(rng.uniform(0.6, 1.6)) * sup / 2.0)


def _style(rng) -> dict:
    """synth_cells._style 에 이 모듈의 버릇 둘 (흘려 쓴 00, 사선 하나의 1) — 난수는 synth_cells 의 것 뒤에 쓴다."""
    s = synth_cells._style(rng)
    s["loop00"] = bool(rng.random() < 0.35)
    s["slash1"] = bool(rng.random() < 0.35)
    return s


def make_reading(rng: np.random.Generator, params: ReadingParams, kind: str, neighbors: bool, label: bool,
                 mode: str, masks: bool = False) -> ReadingCell:
    """칸 하나. masks=True 면 이 칸의 글씨·키 큰 칸의 윗줄 글씨의 잉크 마스크도 (같은 난수 흐름 — 그림은 masks 와 상관없이 같다)."""
    if kind not in KINDS:
        raise ValueError(f"kind 는 {KINDS} 중 하나: {kind}")
    fmt, spec = params.fmt, params.spec
    native = params.source_dpi_ratio if spec.res == "source" else 1.0
    sup = SUPERSAMPLE
    r = native * sup
    cw, ch, ins = params.cell_w, params.cell_h, params.inset
    gh = ch + 2 * ins
    pad = spec.pad_for((0, 0, cw, ch))
    W, H = int(round((cw + 2 * pad) * r)), int(round((ch + 2 * pad) * r))
    canvas = np.zeros((H, W), np.float32)
    vlayer = np.zeros((H, W), np.float32) if masks else None              # 이 칸의 글씨만
    olayer = np.zeros((H, W), np.float32) if masks else None              # 키 큰 칸의 윗줄 글씨만

    def X(v: float) -> float:
        return (v + pad) * r

    def Y(v: float) -> float:
        return (v + pad) * r

    jit = lambda s: rng.uniform(-s, s)                                     # noqa: E731
    line_w = max(1, int(round(rng.uniform(0.8, 1.6) * r)))
    for y in (-ins, ch + ins):                                             # 괘선 (조금 어긋나고 기울어진)
        cv2.line(canvas, (int(X(-pad - 5)), int(Y(y + jit(1.2)))), (int(X(cw + pad + 5)), int(Y(y + jit(1.2)))),
                 float(rng.uniform(0.75, 1.0)), line_w, cv2.LINE_AA)
    for x in (-ins, cw + ins):
        cv2.line(canvas, (int(X(x + jit(1.2))), int(Y(-pad - 5))), (int(X(x + jit(1.2))), int(Y(ch + pad + 5))),
                 float(rng.uniform(0.75, 1.0)), line_w, cv2.LINE_AA)
    style = _style(rng)
    tall = ch >= TALL

    # 인쇄: 계기 칸 왼쪽의 이름표 / 근무 시각 칸 가운데의 ~
    left = 0.0                                                             # 손글씨가 시작할 수 있는 칸 안의 x (템플릿 px)
    if label and fmt == "reading" and cw >= LABEL_MIN_W:                   # 이름표는 넓은 칸에만 (좁은 칸에 이름표와 긴 값은 없다)
        word = str(rng.choice(LABEL_WORDS)) + ":"
        h_l = min(gh * 0.35, ch * 0.8)                                     # 인쇄는 손글씨보다 작다 (손글씨 0.45 배 이상)
        end = _print_text(canvas, word, X(cw * 0.03), Y(ch * 0.5), h_l * r, max(1, int(round(1.2 * r))), 0.9)
        left = end / r - pad + h_l * 0.4
    tilde_x = None
    if label and fmt == "time_range":
        tilde_x = cw * 0.5 + jit(cw * 0.03)
        _tilde(canvas, X(tilde_x), Y(ch * 0.5), r)

    other_box = None
    if neighbors or (tall and rng.random() < 0.6):                         # 이웃 칸의 글씨 — 키 큰 칸은 윗줄(오전 칸)이 자주 들어온다
        sides = ["up", "down", "left", "right"]
        rng.shuffle(sides)
        picks = sides[: int(rng.integers(1, 4))] if neighbors else ["up"]
        if kind == "spill" and not {"up", "down"} & set(picks):           # 이웃 글씨만 있는 빈 칸 — 칸 안에 들어온 잉크가 있게
            picks.append("up" if rng.random() < 0.5 else "down")
        for side in picks:
            ntext, _v = random_value(rng, fmt if fmt != "time_range" else "time", MODES[fmt if fmt != "time_range" else "time"][0][0])
            nst = synth_cells._style(rng)
            if side in ("up", "down"):
                # 윗줄·아랫줄의 값: 그 칸의 가운데에 이 칸의 값과 같은 크기로 — 크게 쓴 것(빈 칸 "spill" 은 늘)만 괘선을 넘어 들어온다
                big = kind == "spill" or rng.random() < 0.3
                if tall:
                    hn = ch * rng.uniform(*params.tall_h) * (1.4 if big else 1.0)
                else:
                    hn = gh * (rng.uniform(1.0, 1.3) if big else rng.uniform(*params.value_h))
                cy = ch * 0.5 + (-gh if side == "up" else gh) + jit(gh * 0.1)
                box = _write(canvas, ntext, hn * r, X(cw * 0.5 + jit(cw * 0.25)), Y(cy), rng, nst,
                             also=olayer if tall and side == "up" else None)
                if tall and side == "up":
                    other_box = box
            else:
                hn = (ch * rng.uniform(0.2, 0.3)) if tall else gh * rng.uniform(0.6, 1.0)
                ink, m, total = _sized_ink(ntext, hn * r, nst, rng)
                # 옆 칸 값의 끝이 이 칸의 여유에 걸친다 — 괘선을 넘어 칸 안으로는 몇 px 까지 (마지막 글자의 무게 중심은 칸 밖)
                if side == "left":
                    x_end = X(rng.uniform(-pad * 0.6, min(cw * 0.02, 6.0)))
                    _put(canvas, ink, x_end - m - total, Y(ch * 0.5 + jit(gh * 0.15)))
                else:
                    x_start = X(cw - rng.uniform(-pad * 0.6, min(cw * 0.02, 6.0)))
                    _put(canvas, ink, x_start - m, Y(ch * 0.5 + jit(gh * 0.15)))

    text, value, ink_box = "", "", None
    if tall:
        h = ch * rng.uniform(*params.tall_h)
        # 인쇄된 ~ 가 있으면 그 양옆에 쓴다 — 같은 줄 (~ 는 칸 가운데). 없으면 칸 높이의 0.3–0.7 어디든
        cy = ch * (0.5 + jit(0.06)) if tilde_x is not None else ch * rng.uniform(0.3, 0.7)
    else:
        h = gh * rng.uniform(*params.value_h)
        cy = ch * 0.5 + jit(gh * 0.1)
    if kind in ("value", "overwrite", "scribble"):
        written, value = random_value(rng, fmt, mode or MODES[fmt][0][0])
        wide = bool(rng.random() < params.p_wide)
        slack = cw * 0.005                                                 # 값의 폭은 칸 폭의 1.01 배까지 (1절 나) — 양옆으로 반씩
        if tilde_x is not None:                                            # 인쇄된 ~ 의 양옆에: 시작은 왼쪽, 끝은 오른쪽
            a, b = written.replace("-", "~").split("~", 1)
            gap = TILDE_W / 2 + 10
            ba = _write(canvas, a, h * r, X(tilde_x - gap - (tilde_x - gap) * rng.uniform(0.35, 0.5)), Y(cy), rng, style,
                        max_w=(tilde_x - gap) * r, also=vlayer, within=(X(-slack), X(tilde_x - gap)))
            bb = _write(canvas, b, h * r, X(tilde_x + gap + (cw - tilde_x - gap) * rng.uniform(0.35, 0.5)), Y(cy), rng, style,
                        max_w=(cw - tilde_x - gap) * r, also=vlayer, within=(X(tilde_x + gap), X(cw + slack)))
            written = f"{a}~{b}"
            ink_box = _union(ba, bb)
        else:
            room = cw - left
            cx = left + room * 0.5 + jit(room * 0.12)
            fit = room * float(rng.uniform(0.93, 1.0)) * r if wide else None
            if fit is not None:
                cx = left + room * 0.5
            ink_box = _write(canvas, written, h * r, X(cx), Y(cy), rng, style, fit_w=fit, max_w=room * 1.01 * r, also=vlayer,
                             within=(X(left - slack), X(cw + slack)))
        if kind == "value":
            text = written
        elif kind == "overwrite":                                         # 한 자리에 다른 숫자를 덧쓴다 — 무엇인지 모른다 (거절)
            text, value = "?", ""
            if ink_box is not None:
                bx0, by0, bx1, by1 = ink_box
                k = rng.uniform(0.2, 0.8)
                _write(canvas, str(int(rng.integers(0, 10))), (by1 - by0) * 0.9, bx0 + (bx1 - bx0) * k, (by0 + by1) / 2, rng,
                       style)
        else:                                                             # 덧칠해 지운 것 — 빈 칸. 쓴 글씨 전체를 덮는다
            bx0, by0, bx1, by1 = ink_box or (X(left), Y(cy) - h * r / 2, X(cw), Y(cy) + h * r / 2)
            for k in range(max(1, int(math.ceil((bx1 - bx0) / max(by1 - by0, 1.0) / 1.2)))):
                seg = (bx1 - bx0) / max(1, int(math.ceil((bx1 - bx0) / max(by1 - by0, 1.0) / 1.2)))
                synth_cells._scribble(canvas, bx0 + seg * (k + 0.5), (by0 + by1) / 2, (by1 - by0) * 1.05, rng, style)
            text, value = "", ""
    elif kind == "x":
        s = min(gh * rng.uniform(0.6, 1.2), ch * 1.1)
        a = s * rng.uniform(0.6, 1.4)
        cx = cw * 0.5 + jit(cw * 0.2)
        th = max(1, int(round(style["thick"] * r)))
        c = style["ink"]
        cv2.line(canvas, (int(X(cx - a / 2)), int(Y(cy - s / 2))), (int(X(cx + a / 2)), int(Y(cy + s / 2))), c, th, cv2.LINE_AA)
        cv2.line(canvas, (int(X(cx + a / 2)), int(Y(cy - s / 2))), (int(X(cx - a / 2)), int(Y(cy + s / 2))), c, th, cv2.LINE_AA)
    elif kind == "note":
        words = " ".join(str(rng.choice(synth_cells.NOTE_WORDS)) for _ in range(int(rng.integers(1, 3))))
        hn = (ch * rng.uniform(0.25, 0.4)) if tall else gh * rng.uniform(0.6, 1.0)
        synth_cells._draw_text_line(canvas, words, X(rng.uniform(-0.3, 0.4) * cw), Y(-ins + rng.uniform(-0.15, 0.65) * gh),
                                    hn * r, rng, style)
    # spill: 가운데 칸에는 쓰지 않는다 (이웃 글씨만). printed: 인쇄만. blank: 아무것도 없다

    img = _scan(canvas, rng, style, sup)
    nat = cv2.resize(img, (max(1, round((cw + 2 * pad) * native)), max(1, round((ch + 2 * pad) * native))),
                     interpolation=cv2.INTER_AREA)
    nat = synth_cells._jpeg(synth_cells._grain(nat, rng), rng)
    out_w, out_h = params.out_size()
    if (nat.shape[1], nat.shape[0]) != (out_w, out_h):
        nat = cv2.resize(nat, (out_w, out_h), interpolation=cv2.INTER_CUBIC if out_w > nat.shape[1] else cv2.INTER_AREA)
    k = float(spec.scale) / r                                              # 캔버스 화소 → 크롭 화소

    def scaled(b):
        return None if b is None else tuple(float(v * k) for v in b)

    def mask(layer):                                                       # 그림과 같은 크기로 줄인 잉크 (> 0.3)
        if layer is None:
            return None
        return cv2.resize(layer, (out_w, out_h), interpolation=cv2.INTER_AREA) > 0.3

    return ReadingCell(nat, text, value if kind == "value" else "", fmt, kind, mode if kind == "value" else "",
                       bool(label), bool(neighbors), params.cell_in_crop(), scaled(ink_box), scaled(other_box),
                       mask(vlayer if kind == "value" else None), mask(olayer))


def _union(a, b):
    if a is None or b is None:
        return a or b
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def make_readings(n: int, seed: int = 0, params: ReadingParams | None = None) -> list[ReadingCell]:
    """한 형식·한 칸 크기의 칸 n 개. 칸 i 는 (seed, i) 로만 정해진다 — 같은 씨앗이면 같은 칸."""
    params = params or ReadingParams()
    return [make_reading(np.random.default_rng([seed, i]), params, *job)
            for i, job in enumerate(plan_readings(n, seed, params))]


# ── export-crops 꼴의 폴더 ─────────────────────────────────────────────────
CROP_GEOMETRY = {"reading": [(362, 38)], "time_range": [(432, 52)], "decimal": [(192, 82)], "time": [(212, 82)]}
VERDICT_OF = {"value": "value", "overwrite": "illegible"}          # 그 밖의 종류는 빈 칸 (empty)


def write_reading_crops(out, n: int, seed: int = 0, *, days: int = 10, start: str = "2030-03-01",
                        geometry: dict[str, list[tuple[int, int]]] | None = None, spec: CropSpec | None = None,
                        split: str = "train", shares: dict[str, float] | None = None) -> dict:
    """`review export-crops` 가 만드는 폴더와 같은 모양의 합성 폴더 (실제를 흉내 낸다 — 4.6 의 학습 시험):
    OUT/<split>/handwritten_number/*.png + OUT/<split>/labels.jsonl. 정답은 검수처럼 **정규화한 값**(text — 적힌 글자가 아니다),
    덧쓴 숫자는 illegible, 빈 칸은 empty. 칸 n 개를 형식·칸 크기(geometry, 기본 CROP_GEOMETRY)마다 나누고(plan_set) 날짜 days 개에
    돌려 가며 둔다. 같은 인자면 같은 바이트. 돌려주는 값: {"written", "by_format", "by_verdict", "dates"}."""
    import json
    from datetime import date, timedelta
    from pathlib import Path

    from ..imaging.io import imwrite

    out = Path(out)
    spec = spec or CropSpec("source", 1.5, None)
    geometry = geometry or CROP_GEOMETRY
    d0 = date.fromisoformat(start)
    dates = [(d0 + timedelta(days=d)).isoformat() for d in range(days)]
    lines, by_format, by_verdict = [], {}, {}
    j = 0
    for g, (fmt, (w, h), k) in enumerate(plan_set(n, seed, geometry, shares)):
        params = ReadingParams(fmt=fmt, cell_w=w, cell_h=h, spec=spec)
        seed_g = seed * 100 + g
        for i, job in enumerate(plan_readings(k, seed_g, params)):
            c = make_reading(np.random.default_rng([seed_g, i]), params, *job)
            day = dates[j % len(dates)]
            verdict = VERDICT_OF.get(c.kind, "empty")
            fid = f"synthr-{day}-{g:02d}{i:05d}:meter:{fmt}:{i}"
            rel = Path(split) / "handwritten_number" / f"synthr-{day}-{g:02d}{i:05d}.png"
            imwrite(out / rel, c.image)
            x0, y0 = 100, 200
            lines.append({"field_id": fid, "file": rel.as_posix(), "text": c.value if verdict == "value" else "",
                          "verdict": verdict, "template": "synth_readings", "region": "meter", "field_name": fmt,
                          "row_key": str(i), "kind": "handwritten_number", "format": fmt, "work_date": day, "split": split,
                          "spec": spec.to_dict(), "inked": True, "resolution": spec.res, "out_scale": float(spec.scale),
                          "pad": spec.pad_for((x0, y0, x0 + w, y0 + h)), "bbox": [x0, y0, x0 + w, y0 + h],
                          "reviewer": "synthetic", "reviewed_at": f"{day}T00:00:00"})
            by_format[fmt] = by_format.get(fmt, 0) + 1
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
            j += 1
    (out / split).mkdir(parents=True, exist_ok=True)
    with open(out / split / "labels.jsonl", "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    return {"written": len(lines), "by_format": by_format, "by_verdict": by_verdict, "dates": dates}
