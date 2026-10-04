"""합성 메타 필드: 일보의 차량번호(네 자리)·작성자(이름)·날짜의 월·일을 쓰는 사람마다 다른 손글씨로 (tasks/0004 단계 2–4).

실제 일보에서 본 것 (3일치 30쪽): 차량번호는 전부 네 자리 숫자이고 여러 번호가 앞 두 자리가 같다. 인쇄된 "차량번호:" 뒤에 크게,
띄엄띄엄 쓴다. 작성자는 날마다 같은 사람이 자기 이름을 같은 글씨로 쓴다. 날짜는 월·일만 손으로 쓴다.

그래서 여기서 그리는 글씨는:
  · 쓰는 사람마다 고정된 버릇(writer_style: 숫자의 꼴, 기울기, 굵기, 크기, 글자 사이, 진하기)에 쪽마다 작은 흔들림(page_style)
  · 글자는 자체 획(tools/handfont.py) — OpenCV 내장 글꼴을 쓰지 않으므로 OpenCV 판과 무관하다 (tasks/0004 4.8)
  · 이름은 영문 소문자를 이어 쓴 꼴(handfont.draw_word). 정답 값은 대문자 (ALPHA …) — 라벨과 같은 표기

두 곳에서 쓴다.
  · tools/synth.py 의 선택 기능 meta_fields: 합성 일보 쪽에 이 필드를 그린다 (render_field_ink → 쪽에 합성)
  · make_field_crop: 파이프라인이 그 필드를 규격대로 뜬 크롭과 비슷한 그림을 쪽을 만들지 않고 바로 (학습용 합성 셀과 시험)
    쪽(200 dpi) → JPEG → 원본 해상도로 렌더링 → 셀만 정합, 의 거친 정도를 흉내 낸다.

이 데이터로 잴 수 있는 것은 경로·논리다. 한글 이름·실제 차량번호의 인식률이 아니다 (CLAUDE.md).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import cv2
import numpy as np

from ..imaging.cropspec import CropSpec
from . import handfont
from .synth_cells import _elastic, _grain, _ink_width, _jpeg, _paste

# 합성 현장의 사람과 차 — 전부 가상의 값. 앞 두 자리가 같은 번호가 여럿이다 (실제 일보처럼)
ROSTER = ("ALPHA", "BRAVO", "CHARLIE", "DELTA", "FOXTROT", "KILO")         # 시험용 분류기가 아는 사람
STRANGERS = ("ECHO", "MIKE", "OSCAR", "ROMEO", "VICTOR")                   # 학습에 없는 사람
VEHICLES = ("4127", "4183", "4135", "5260", "4172", "5216")               # 시험용 숫자 모델·목록이 아는 차
NEW_VEHICLES = ("7391", "6048", "3825")                                   # 목록에 없는 차 (새 차)
META_KEYS = ("vehicle_no", "operator", "date.month", "date.day")
LETTERS = set("abcdefhiklmnoprstuvwx")                                     # handfont 에 있는 소문자 (이름은 이 글자로만)


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def writer_style(name: str) -> dict:
    """한 사람의 고정된 버릇. 같은 이름이면 언제나 같다."""
    rng = np.random.default_rng(_seed(f"writer:{name}"))
    return {"variants": handfont.random_variants(rng),
            "thick": float(rng.uniform(1.3, 2.8)),          # 획 굵기 (글자 높이 22 px 기준)
            "slant": float(rng.uniform(-0.45, 0.12)),        # 음수 = 앞으로 기울임. 뒤로는 조금만 (7 이 1 처럼 보이지 않게)
            "ink": float(rng.uniform(0.7, 1.0)),
            "size": float(rng.uniform(0.62, 0.9)),           # 글자 높이 / 필드 높이
            "gap": float(rng.uniform(0.12, 0.7)),            # 숫자 사이 / 글자 높이 — 크게 띄엄띄엄 쓰는 사람이 있다
            "letter_gap": float(rng.uniform(0.0, 0.25)),     # 이름 글자 사이
            "x0": float(rng.uniform(0.02, 0.25)),            # 필드 왼쪽에서 쓰기 시작하는 자리 (필드 폭 대비)
            "dy": float(rng.uniform(-0.12, 0.12))}           # 필드 가운데에서 위아래로


def page_style(style: dict, rng: np.random.Generator) -> dict:
    """같은 사람이라도 쪽마다 조금씩 다르게."""
    s = dict(style)
    s["slant"] = style["slant"] + float(rng.uniform(-0.05, 0.05))
    s["thick"] = style["thick"] * float(rng.uniform(0.9, 1.1))
    s["size"] = style["size"] * float(rng.uniform(0.93, 1.07))
    s["gap"] = max(0.05, style["gap"] * float(rng.uniform(0.85, 1.15)))
    s["x0"] = min(0.4, max(0.0, style["x0"] + float(rng.uniform(-0.05, 0.05))))
    s["dy"] = style["dy"] + float(rng.uniform(-0.06, 0.06))
    s["ink"] = min(1.0, style["ink"] * float(rng.uniform(0.9, 1.05)))
    return s


def random_style(rng: np.random.Generator) -> dict:
    """이름 없는 아무나 (숫자 모델의 합성 학습 셀)."""
    return page_style(writer_style(f"anon:{int(rng.integers(1 << 30))}"), rng)


def written_name(value: str) -> str:
    """정답 값(대문자) → 종이에 쓰는 꼴 (소문자, handfont 에 있는 글자만)."""
    return "".join(c for c in value.lower() if c in LETTERS) or "x"


# ── 그리기 (잉크 마스크) ────────────────────────────────────────────────────
def render_field_ink(text: str, kind: str, box_w: int, box_h: int, margin: int, r: float, style: dict,
                     rng: np.random.Generator) -> np.ndarray:
    """필드 하나의 잉크(0–1). 캔버스 = 필드 상자 + 둘레 margin (템플릿 px), r = 템플릿 px 당 화소.
    kind: digits(띄엄띄엄 숫자) | name(이어 쓴 이름). 글씨는 상자를 조금 넘을 수 있다 (상자는 인쇄된 칸이 아니다)."""
    W, H = int(round((box_w + 2 * margin) * r)), int(round((box_h + 2 * margin) * r))
    ink = np.zeros((H, W), np.float32)
    if not text:
        return ink
    h = box_h * style["size"] * r
    x = (margin + box_w * style["x0"]) * r
    cy = (margin + box_h * (0.5 + style["dy"])) * r
    if kind == "name":
        g = handfont.draw_word(written_name(text), h * 0.95, style, rng)
        g = _elastic(g, rng, alpha=h * 0.03, sigma=max(2.0, h * 0.1))
        w = _ink_width(g)
        _paste(ink, g, x + w / 2, cy, style["ink"])
        return ink
    gap = style["gap"] * h
    for ch in text:
        g = handfont.draw_glyph(ch, h * float(rng.uniform(0.92, 1.08)), style, rng)
        g = _elastic(g, rng, alpha=h * 0.05, sigma=max(2.0, h * 0.12))
        w = _ink_width(g)
        _paste(ink, g, x + w / 2, cy + rng.uniform(-0.06, 0.06) * h, style["ink"] * float(rng.uniform(0.88, 1.0)))
        x += w + gap
    return ink


def printed_fragments(ink: np.ndarray, r: float, margin: int, box_w: int, box_h: int, rng: np.random.Generator) -> None:
    """필드 둘레의 인쇄된 것: 밑줄, 칸 테두리 조각, 왼쪽에 걸친 인쇄 글자의 꼬리 (모델이 무시해야 하는 것)."""
    H, W = ink.shape
    u = rng.random()
    if u < 0.5:                                               # 밑줄
        y = int(round((margin + box_h * rng.uniform(0.8, 1.05)) * r))
        cv2.line(ink, (0, y), (W, y), float(rng.uniform(0.6, 0.95)), max(1, int(round(r * rng.uniform(0.8, 1.6)))))
    elif u < 0.7:                                             # 칸 테두리
        x0, y0 = int(round(margin * r * rng.uniform(0.3, 1.0))), int(round(margin * r * rng.uniform(0.3, 1.0)))
        cv2.rectangle(ink, (x0, y0), (W - x0, H - y0), float(rng.uniform(0.6, 0.95)), max(1, int(round(r))))
    if rng.random() < 0.35:                                   # 왼쪽 끝에 걸친 인쇄 글자 조각 (예: "번호:" 의 꼬리)
        st = {"variants": {}, "thick": 1.2, "slant": 0.0}
        g = handfont.draw_word(str(rng.choice(["no", "on", "ri", "ate", "ay"])), box_h * r * 0.35, st,
                               np.random.default_rng(int(rng.integers(1 << 30))))
        _paste(ink, g, -_ink_width(g) * rng.uniform(0.0, 0.4), (margin + box_h * 0.6) * r, 0.9)


# ── 크롭 (학습·시험용) ─────────────────────────────────────────────────────
@dataclass
class FieldSpec:
    """필드의 모양: 상자(템플릿 px, doc_field 의 bbox 크기)와 크롭 규격."""

    box_w: int = 390
    box_h: int = 65
    spec: CropSpec = CropSpec("source", 1.5, 8)
    source_dpi_ratio: float = 1.5

    def out_size(self) -> tuple[int, int]:
        p = self.spec.pad_for((0, 0, self.box_w, self.box_h))
        s = float(self.spec.scale)
        return max(1, round((self.box_w + 2 * p) * s)), max(1, round((self.box_h + 2 * p) * s))


def make_field_crop(rng: np.random.Generator, text: str, kind: str, fspec: FieldSpec, style: dict | None = None,
                    printed: bool = True) -> np.ndarray:
    """필드 하나의 크롭 (회색조 uint8, fspec.out_size()). 파이프라인의 크롭처럼: 200 dpi 쪽에 그린 글씨가 JPEG 를 거쳐
    원본 해상도로 렌더링되고, 정합이 몇 px 어긋난다."""
    style = style or random_style(rng)
    pad = fspec.spec.pad_for((0, 0, fspec.box_w, fspec.box_h))
    margin = pad + 6                                          # 정합 오차만큼 더 그려 두고 나중에 자른다
    sup = 2.0
    ink = render_field_ink(text, kind, fspec.box_w, fspec.box_h, margin, sup, style, rng)
    if printed:
        printed_fragments(ink, sup, margin, fspec.box_w, fspec.box_h, rng)
    # 200 dpi 쪽으로: 종이·잉크 → 회색, 흐림, 잡음, JPEG (synth.scan_effect·_write_pdf 와 같은 거친 정도)
    w200, h200 = fspec.box_w + 2 * margin, fspec.box_h + 2 * margin
    g = cv2.resize(ink, (w200, h200), interpolation=cv2.INTER_AREA)
    paper, dark = float(rng.uniform(232, 250)), float(rng.uniform(15, 70))
    img = paper - np.clip(g, 0, 1) * (paper - dark)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    img = _grain(img, rng)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE) if ok else img
    # 원본 해상도로 렌더링 + 셀만 정합: 정합 오차(이동 ±2 px, 회전 ±0.6°)를 넣고 크롭 상자만큼 자른다
    s = float(fspec.spec.scale)
    ang = float(rng.uniform(-0.6, 0.6))
    dx, dy = rng.uniform(-2, 2, 2)
    m = cv2.getRotationMatrix2D((w200 / 2, h200 / 2), ang, 1.0)
    m[:, 2] += [dx - (margin - pad), dy - (margin - pad)]     # 크롭 상자의 왼쪽 위가 원점
    m = m * s
    out_w, out_h = fspec.out_size()
    out = cv2.warpAffine(img, m, (out_w, out_h), flags=cv2.INTER_CUBIC, borderValue=int(paper))
    return _jpeg(out, rng) if fspec.spec.res == "aligned" else out


def kind_of(meta_key: str) -> str:
    return "name" if meta_key == "operator" else "digits"


def random_text(rng: np.random.Generator, meta_key: str) -> str:
    """숫자 모델의 합성 학습 값: 차량번호는 네 자리(앞자리 0 없음), 월·일은 한두 자리 (가끔 07 처럼 0 을 붙여 쓴다)."""
    if meta_key == "date.month":
        v = int(rng.integers(1, 13))
    elif meta_key == "date.day":
        v = int(rng.integers(1, 32))
    else:
        return str(int(rng.integers(1000, 10000)))
    return f"{v:02d}" if v < 10 and rng.random() < 0.25 else str(v)
