"""소수·시각 모델의 추론 쪽 — torch 없이 돈다 (tasks/0011 4.3).

숫자 모델(recognize/digits)과 같은 망(합성곱·배치 정규화·ReLU·최대 풀링, CTC)과 같은 학습 코드, 다른 글자·입력·답:
  글자  blank + 0123456789.:~-?        답: 적힌 글자 · 빈 칸 "" · 거절 "?" (ADR 0012 의 셋)
  입력  글씨 띠(band) → 높이 48 × 폭 320, 잉크·세로 위치·가로 위치 채널. 가로 80칸의 CTC (근무 시각 11글자에 넉넉하다)

글씨 띠 (band — 학습·추론·평가·틀린 칸 모아 보기가 같이 쓴다). 크롭(숫자 모델의 기본 규격 — 원본 ×1.5, 여유 = 행 높이의 절반) 안에서:
  1. 칸 자리: CellContext.bbox + 카드의 규격 (내보낸 줄은 bbox·pad·out_scale, 합성 칸은 생성기가 안다) — cell_in_crop
  2. 이진화(grid.binarize)·괘선 지우기(cells.remove_rules) — 숫자 칸과 같은 구현
  3. 무게 중심이 칸 안(가로·세로)에 있는 잉크 덩어리(면적 4 px 이상, 템플릿 px — 배율로 환산) — 괘선을 넘은 글씨는 들어오고, 윗줄·
     아랫줄과 옆 칸에서 넘어온 글씨는 빠진다. 옆 칸 값이 세로 괘선에서 끊겨 칸 안에 남은 조각(칸의 왼쪽·오른쪽 끝에서 side_in 8 px 안에
     다 든 덩어리)도 뺀다 — 무게 중심은 칸 안이지만 이 칸의 글씨가 아니다 (합성 키 큰 칸에서 이것이 줄을 늘려 띠가 글씨의 2.3 배가 되었다)
  4. 덩어리를 줄로 묶는다 (세로 구간이 짧은 쪽 높이의 30 % 넘게 겹치면 한 줄, 면적이 큰 것부터) → 잉크가 가장 많은 줄의 세로 범위
     ± 그 높이의 15 %(최소 3 px). 덩어리가 없으면 칸의 세로 범위. 줄의 세로 범위는 덩어리의 상자가 아니라 그 줄 잉크의 위·아래 1 %(trim)를
     뺀 행까지다 — 글씨에 붙은 이웃 글씨의 가는 꼬리 하나가 줄을 늘리지 않게 (깎인 끝은 여유가 다시 덮는다).
     괘선에서 끊긴 조각: 괘선 지우기는 괘선을 넘은 획을 괘선 자리에서 끊는다. 칸 안 조각(무게 중심이 칸 안)과 칸 밖 조각이 괘선 두께
     (join_gap, 템플릿 px) 안에서 마주 보고 마주 보는 끝의 열이 겹치면 한 획이 끊긴 것이다. 칸 안 조각이 칸 높이의 tail_max(40 %)보다
     낮으면 윗줄·아랫줄 글씨가 넘어온 꼬리라 3 에서 뺀다 — 이 칸의 글씨가 괘선을 넘은 것은 칸 안 부분이 칸 높이의 절반을 넘는다
     (글씨는 칸 가운데에 쓴다). 짝(몸통)이 크롭 밖이라 보이지 않아도 칸의 위·아래 끝에 닿은 낮은 조각은 꼬리다. 그 밖이면 이 칸의 글씨가
     넘은 것이라 칸 밖 조각까지 띠를 넓힌다 — 그 줄 높이의 over_h(30 %) 이하인 조각만, 줄 높이의 over_max(25 %)까지 (이웃의 꼬리가 이 칸의
     숫자에 붙어 한 덩어리가 되면 그 바깥 몸통이 "넘은 조각"으로 보인다). 넓힌 쪽에는 여유를 더 얹지 않는다 — 띠는 줄 높이의 1.5 배를
     넘지 않는다. 합성 칸에서 넘은 조각을 잇지 않으면 칸보다 큰 글씨의 칸 다섯 중 하나꼴로 글씨 잉크의 85–90 % 만 띠에 들어왔고, 이웃의
     꼬리가 줄에 묶이면 띠가 글씨의 두 배까지 커졌다 (tasks/0011 12절)
  5. 가로는 칸 ± 칸 높이의 1/4 (최소 4 px, 템플릿 px) — 크롭의 옆 여유(행 높이의 절반)에는 옆 칸의 값이 들어온다
  6. 높이를 48 로 비율을 지켜 줄이고 왼쪽에 붙여 폭 320 까지 종이색(그 그림의 중앙값)으로 채운다 — 넓으면 폭을 320 으로 줄인다
  7. 잉크(숫자 모델의 normalize 의 잉크)·세로 위치(띠의 위·아래가 −1·+1)·가로 위치(칸의 왼쪽·오른쪽이 −1·+1, 그 밖은 그대로 이어서 —
     채운 곳도). 칸 크기마다 가로 배율이 달라도 "칸 안의 어디"가 같은 값이다
  띠는 자리만 정한다 — 모델이 보는 것은 회색 그림 그대로다 (괘선 지우기는 세로획 하나의 "1" 을 지운다 — CLAUDE.md).
  근거 (1절 나 — 실제 가동 일보 40쪽): 크롭 전체를 높이 48 로 줄이면 글씨가 계기 칸 17 px, 근무 시각 7 px, 키 큰 칸 4 px 이었다. 띠로 자르면
  계기 칸 21–36 px(중앙 31), 근무 시각 10–18 px(중앙 12)이고 윗줄의 글씨가 빠진다. 덩어리를 줄로 묶지 않으면 키 큰 칸의 띠가 132 px 까지
  커져 6 px 로 줄었다. 값의 폭은 칸 폭의 0.93–1.01 이다 — 가로 여유는 그 넘침만큼.

해독 — 고르기만 한다 (원칙 4): CTC 빔의 후보 중 그 칸의 형식에 맞는 글자(formats.is_valid)와 빈 칸·거절만 남긴다. 같은 값으로 정규화되는
글자(08:00·8:00)의 확률은 더한다. 답 = 확률이 가장 큰 값, 적힌 글자 = 그 값의 가장 큰 글자, 신뢰도 = 그 값의 확률 — 형식에 맞지 않는
글자의 몫은 다시 나누지 않는다 (신뢰도를 깎는다). 형식에 맞는 답이 빔에 없으면 거절 (신뢰도 0).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ...forms.formats import FormatError, normalize
from ...imaging.cells import remove_rules
from ...imaging.cropspec import CropSpec
from ...imaging.grid import binarize
from ..digits.model import Shape, beam_search, ink_channel, log_softmax, sha256_file

CHARS = "0123456789.:~-?"
REJECT = "?"
INPUT_W, INPUT_H = 320, 48
N_INPUT_CHANNELS = 3
SHAPE = Shape(CHARS, INPUT_W, INPUT_H)
N_CLASSES = SHAPE.n_classes
FORMATS = ("decimal", "time", "time_range", "reading")
READER = "readings"
TOP_K = 5
# 빔: 숫자 모델(12)보다 넓게 — 형식으로 거르고도 후보가 남게 (같은 값의 다른 표기, 형식에 맞지 않는 글자가 빔을 나눠 가진다)
BEAM = 32
# 글씨 띠의 기본값 (위 1–7). 카드에 적고, 카드와 코드가 다르면 읽지 않는다 (load_card)
BAND = {"min_area": 4, "side_in": 8, "overlap": 0.3, "trim": 0.01, "margin": 0.15, "min_margin": 3, "side": 0.25, "min_side": 4,
        "join_gap": 4, "tail_max": 0.4, "over_h": 0.3, "over_max": 0.25, "height": INPUT_H, "width": INPUT_W}
CARD_KEYS = ("name", "created_at", "code_version", "reader", "formats", "spec", "input", "chars", "architecture",
             "train_args", "seed", "data", "validation", "temperature", "auto_accept", "libraries", "model_sha256")
_YS = np.repeat(np.linspace(-1, 1, INPUT_H, dtype=np.float32)[:, None], INPUT_W, axis=1)


# ── 글씨 띠 ────────────────────────────────────────────────────────────────
def cell_in_crop(bbox: tuple[int, int, int, int], spec: CropSpec) -> tuple[float, float, float, float]:
    """크롭(imaging/cropspec.crop_cell) 안의 칸 자리 (크롭 화소). 크롭은 칸 ± 여유를 배율대로 뜬 것이고 쪽 밖은 흰색으로 채우므로
    칸은 늘 같은 자리다."""
    x0, y0, x1, y1 = bbox
    p, s = spec.pad_for(bbox), float(spec.scale)
    return p * s, p * s, (p + x1 - x0) * s, (p + y1 - y0) * s


@dataclass
class Band:
    image: np.ndarray                          # (INPUT_H, INPUT_W) 회색조 uint8 — 모델이 보는 그림
    left: float                                # 칸의 왼쪽·오른쪽 (그림의 열 좌표 — 가로 위치 채널의 −1·+1)
    right: float
    rect: tuple[int, int, int, int]            # 크롭에서 띠의 상자 (x0, y0, x1, y1) — 틀린 칸 모아 보기가 그린다
    lines: int                                 # 덩어리로 묶은 줄의 수 (0 = 덩어리 없음 → 칸의 세로 범위)


def band(gray: np.ndarray, cell: tuple[float, float, float, float], scale: float, params: dict | None = None) -> Band:
    """크롭과 그 안의 칸 자리 → 글씨 띠. scale: 템플릿 px 당 크롭 화소 (규격의 배율 — 면적·최소 여유를 환산한다)."""
    p = BAND if params is None else params
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    gray = np.ascontiguousarray(gray if gray.dtype == np.uint8 else np.clip(gray, 0, 255).astype(np.uint8))
    H, W = gray.shape
    cx0, cy0, cx1, cy1 = cell
    # 2–3. 칸 안에 무게 중심이 있는 잉크 덩어리
    b = remove_rules(binarize(gray))
    n, lab, stats, cents = cv2.connectedComponentsWithStats(b, connectivity=8)
    min_area = p["min_area"] * scale * scale
    keep = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_area]
    edge = p["side_in"] * scale                                  # 옆 칸 값이 세로 괘선에서 끊겨 칸 안에 남은 조각 (칸의 왼쪽·오른쪽 끝에서
    inside = {i for i in keep if cx0 <= cents[i][0] <= cx1 and cy0 <= cents[i][1] <= cy1     # side_in 안에 다 든 덩어리)은 빼다
              and not (stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH] <= cx0 + edge
                       or stats[i, cv2.CC_STAT_LEFT] >= cx1 - edge)}
    tails, over = _cut_pieces(lab, stats, keep, inside, p["join_gap"] * scale, p["tail_max"] * (cy1 - cy0), (cy0, cy1))
    comps = [(int(stats[i, cv2.CC_STAT_AREA]), int(stats[i, cv2.CC_STAT_TOP]),
              int(stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]), i) for i in sorted(inside - tails)]
    # 4. 줄로 묶기 — 면적이 큰 것부터 (같은 면적이면 위에서부터 — 정해진 순서)
    lines: list[list] = []                                       # [위, 아래, 잉크, 덩어리 번호들]
    for area, top, bot, i in sorted(comps, key=lambda c: (-c[0], c[1], c[2], c[3])):
        for ln in lines:
            ov = min(bot, ln[1]) - max(top, ln[0])
            if ov > p["overlap"] * min(bot - top, ln[1] - ln[0]):
                ln[0], ln[1], ln[2] = min(ln[0], top), max(ln[1], bot), ln[2] + area
                ln[3].append(i)
                break
        else:
            lines.append([top, bot, area, [i]])
    if lines:
        best = max(lines, key=lambda ln: (ln[2], -ln[0]))
        top, bot = _trimmed(lab, best, p["trim"])
        lh = bot - top
        m = max(p["margin"] * lh, p["min_margin"])
        y0, y1 = top - m, bot + m
        for i in best[3]:                                        # 이 칸의 글씨가 괘선을 넘은 조각 — 그 줄 높이의 over_h 이하인 조각만,
            for d in over.get(i, ()):                           # 넓히는 것은 줄 높이의 over_max 까지 (이웃의 꼬리가 이 칸의 숫자에 붙어
                if stats[d, cv2.CC_STAT_HEIGHT] > p["over_h"] * lh:   # 한 덩어리가 되면 그 바깥 몸통이 "작은 조각"으로 보인다).
                    continue                                    # 넓힌 쪽에는 여유를 더 얹지 않는다 — 띠는 줄 높이의 1 + 2 × max(여유, over_max) 배를 넘지 않는다
                y0 = min(y0, max(int(stats[d, cv2.CC_STAT_TOP]), top - p["over_max"] * lh))
                y1 = max(y1, min(int(stats[d, cv2.CC_STAT_TOP] + stats[d, cv2.CC_STAT_HEIGHT]), bot + p["over_max"] * lh))
    else:
        y0, y1 = cy0, cy1
    # 5. 가로: 칸 ± 칸 높이의 1/4
    side = max(p["side"] * (cy1 - cy0), p["min_side"] * scale)
    x0, x1 = cx0 - side, cx1 + side
    bx0, by0 = max(0, int(math.floor(x0))), max(0, int(math.floor(y0)))
    bx1, by1 = min(W, int(math.ceil(x1))), min(H, int(math.ceil(y1)))
    if bx1 - bx0 < 2 or by1 - by0 < 2:                           # 칸이 크롭 밖이다 (있을 수 없다 — 그래도 멈추지 않게)
        bx0, by0, bx1, by1 = 0, 0, W, H
    sub = gray[by0:by1, bx0:bx1]
    # 6. 높이 48, 왼쪽에 붙여 폭 320 까지 종이색으로 (넓으면 폭을 320 으로)
    th, tw = p["height"], p["width"]
    k = th / sub.shape[0]
    w = max(1, int(round(sub.shape[1] * k)))
    kx = k if w <= tw else tw / sub.shape[1]
    small = cv2.resize(sub, (min(w, tw), th), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_LINEAR)
    if small.shape[1] < tw:
        paper = int(np.median(sub))
        small = cv2.copyMakeBorder(small, 0, 0, 0, tw - small.shape[1], cv2.BORDER_CONSTANT, value=paper)
    return Band(small, (cx0 - bx0) * kx, (cx1 - bx0) * kx, (bx0, by0, bx1, by1), len(lines))


def _trimmed(lab: np.ndarray, line: list, q: float) -> tuple[int, int]:
    """줄의 세로 범위 — 그 줄 덩어리들의 잉크가 위·아래에서 q(1 %)씩 남는 행까지. 덩어리의 상자로 재면 글씨에 붙은 이웃 글씨의 가는 꼬리
    하나가 줄을 늘린다 (합성 값 칸의 1 % 남짓 — 띠가 글씨의 1.5 배를 넘었다). 깎인 끝은 여유(15 %)가 다시 덮는다."""
    top, bot, _area, ids = line
    if q <= 0:
        return top, bot
    rows = np.isin(lab[top:bot], ids).sum(axis=1).astype(np.float64)
    cum = np.cumsum(rows)
    if cum[-1] <= 0:
        return top, bot
    cum /= cum[-1]
    return top + int(np.searchsorted(cum, q)), top + int(np.searchsorted(cum, 1.0 - q)) + 1


def _cut_pieces(lab: np.ndarray, stats: np.ndarray, keep: list[int], inside: set[int], gap: float, tail_h: float,
                rows: tuple[float, float]) -> tuple[set[int], dict[int, list[int]]]:
    """괘선 자리에서 끊긴 획의 짝 (위 4 의 설명): (윗줄·아랫줄 글씨의 꼬리인 칸 안 조각들 — 높이가 tail_h 보다 낮은 것,
    칸 안 조각 → 이 칸의 글씨가 넘은 칸 밖 조각들). 짝 = 세로로 0–gap 떨어져 마주 보고, 마주 보는 끝 두 줄의 열이 (2 px 너그럽게) 겹친다.
    짝(몸통)이 크롭 밖이라 보이지 않는 꼬리도 있다 — 칸의 위·아래 끝(rows ± gap)에 닿은 낮은 조각은 짝이 없어도 꼬리다."""
    def box(i):
        x, y, w, h = (int(v) for v in stats[i, :4])
        return x, y, x + w, y + h

    def edge_cols(i, top: bool) -> np.ndarray:
        x0, y0, x1, y1 = box(i)
        rows = lab[y0:min(y0 + 2, y1), x0:x1] if top else lab[max(y1 - 2, y0):y1, x0:x1]
        cols = np.nonzero((rows == i).any(axis=0))[0] + x0
        return cols

    tails: set[int] = set()
    over: dict[int, list[int]] = {}
    outside = [d for d in keep if d not in inside]
    for c in sorted(inside):
        cb = box(c)
        if stats[c, cv2.CC_STAT_HEIGHT] < tail_h and (cb[1] <= rows[0] + gap or cb[3] >= rows[1] - gap):
            tails.add(c)
            continue
        for d in outside:
            db = box(d)
            if min(cb[2], db[2]) - max(cb[0], db[0]) <= 0:
                continue
            if 0 <= cb[1] - db[3] <= gap:                       # d 가 위
                a, z = edge_cols(c, True), edge_cols(d, False)
            elif 0 <= db[1] - cb[3] <= gap:                     # d 가 아래
                a, z = edge_cols(c, False), edge_cols(d, True)
            else:
                continue
            if not len(a) or not len(z) or np.min(np.abs(a[:, None] - z[None, :])) > 2:
                continue
            if stats[c, cv2.CC_STAT_HEIGHT] < tail_h:
                tails.add(c)
            else:
                over.setdefault(c, []).append(d)
    return tails, over


def x_channel(left: float, right: float, width: int = INPUT_W) -> np.ndarray:
    """가로 위치 채널 (INPUT_H, width): 칸의 왼쪽·오른쪽이 −1·+1, 그 밖은 그대로 이어서."""
    cols = np.arange(width, dtype=np.float32) + 0.5
    span = max(float(right - left), 1.0)
    row = (-1.0 + 2.0 * (cols - float(left)) / span).astype(np.float32)
    return np.repeat(row[None, :], INPUT_H, axis=0)


def channels(b: Band) -> np.ndarray:
    """띠 → (3, H, W) float32: 잉크, 세로 위치, 가로 위치."""
    return np.stack([ink_channel(b.image), _YS, x_channel(b.left, b.right)]).astype(np.float32)


def preprocess(crop: np.ndarray, cell: tuple[float, float, float, float], scale: float) -> tuple[np.ndarray, Band]:
    """크롭 하나 → (모델 입력 (1, 3, H, W), 띠)."""
    b = band(crop, cell, scale)
    return channels(b)[None], b


# ── 답 ────────────────────────────────────────────────────────────────────
def encode(text: str) -> list[int]:
    return [CHARS.index(c) + 1 for c in text]


@dataclass(frozen=True)
class Answer:
    kind: str                # value | empty | reject
    value: str               # 정규화한 값 (빈 칸·거절은 "")
    text: str                # 적힌 글자 — 그 값의 가장 큰 글자 (빈 칸 "", 거절 "?")
    prob: float


def decode(logits: np.ndarray, fmt: str, temperature: float = 1.0, beam: int = BEAM, top: int = TOP_K) -> list[Answer]:
    """(C, T) 점수 → 형식에 맞는 답들 (확률순, 최대 top). 형식에 맞는 답이 빔에 없으면 [거절 (신뢰도 0)]."""
    return decode_beams(beam_search(log_softmax(logits, temperature), beam=beam, chars=CHARS), fmt, top)


def decode_beams(beams: list[tuple[str, float]], fmt: str, top: int = TOP_K) -> list[Answer]:
    """빔의 (글자, 로그 확률) → 형식으로 고른 답. 같은 값의 글자는 확률을 더하고, 형식에 맞지 않는 글자는 버린다 (몫을 나누지 않는다)."""
    groups: dict[tuple[str, str], list] = {}                       # (종류, 값) → [확률 합, 가장 큰 글자, 그 확률]
    for s, lp in beams:
        p = math.exp(lp)
        if s == "":
            key = ("empty", "")
        elif REJECT in s:
            key = ("reject", "")
        else:
            try:
                key = ("value", normalize(fmt, s))
            except FormatError:
                continue
        g = groups.setdefault(key, [0.0, s, -1.0])
        g[0] += p
        if p > g[2]:
            g[1], g[2] = s, p
    ranked = sorted(groups.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = [Answer(k, v, "" if k == "empty" else REJECT if k == "reject" else g[1], float(g[0])) for (k, v), g in ranked]
    return out[:top] or [Answer("reject", "", REJECT, 0.0)]


def shape_of(text: str) -> str:
    """값의 모양 — 숫자를 d 로 (1234.5 → dddd.d). 카드·보고에 값 대신 적는다 (tasks/0011 7절)."""
    return "".join("d" if c.isdigit() else c for c in text)


# ── 카드 ──────────────────────────────────────────────────────────────────
def reader_of(card: dict) -> str:
    """카드의 갈래: readings | meta | digits. 모델 폴더를 읽는 곳은 이것을 먼저 본다 (4.3)."""
    if card.get("reader") == READER:
        return READER
    return "meta" if card.get("meta") else "digits"


def load_card(model_dir: str | Path, check_hash: bool = True) -> dict:
    d = Path(model_dir)
    card = json.loads((d / "card.json").read_text(encoding="utf-8"))
    kind = reader_of(card)
    if kind != READER:
        where = "[recognize.meta]" if kind == "meta" else "[recognize.digits]"
        what = "메타 필드 모델" if kind == "meta" else "숫자 모델"
        raise ValueError(f"'{card.get('name', d.name)}' 은 {what}입니다 — [recognize.readings] 가 아니라 {where} 에 꽂으세요")
    lacking = [k for k in CARD_KEYS if k not in card]
    if lacking:
        raise ValueError(f"모델 카드에 항목이 없습니다: {lacking} ({d / 'card.json'})")
    if card["chars"] != CHARS:
        raise ValueError(f"모델 카드의 문자({card['chars']!r})가 이 코드({CHARS!r})와 다릅니다 — 이 버전으로 다시 학습하세요")
    inp = card["input"]
    if (inp.get("width"), inp.get("height"), inp.get("channels")) != (INPUT_W, INPUT_H, N_INPUT_CHANNELS) or inp.get("band") != BAND:
        raise ValueError(f"모델 카드의 입력·글씨 띠가 이 코드와 다릅니다 ({INPUT_W}×{INPUT_H}×{N_INPUT_CHANNELS}, 띠 {BAND}) — "
                         "이 버전으로 다시 학습하세요")
    bad = [f for f in card["formats"] if f not in FORMATS]
    if bad or not card["formats"]:
        raise ValueError(f"모델 카드의 형식이 {FORMATS} 가 아닙니다: {card['formats']}")
    if check_hash and sha256_file(d / "model.onnx") != card["model_sha256"]:
        raise ValueError(f"model.onnx 가 카드에 적힌 것(SHA-256)과 다릅니다: {d}")
    return card
