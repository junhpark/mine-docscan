"""합성 장비 가동 일보 두 종 (tasks/0005 4.6, 단계 6) — 개인정보 없는 시험용. tools/synth.py 의 선택 기능 usage_logs 가 부른다.

  synth_usage_log    세로 — 중기운행일보·점보 작업일보와 같은 짜임: 머리(장비명·운전자·서명) / 작업 표(activities) /
                     계기 칸(meter: 시작·종료·총, 낮은 칸) / 정비 및 특이사항(글자 필드)
  synth_loader_log   가로 — 로우더 작업일보와 같은 짜임: 제목 앞 괄호에 장비명 / 작업량 표(tally: 구분 7행 × 하단·저광장,
                     갑·연근·소계 — 하단·저광장은 인쇄된 괘선이 없는 나눔 선 split_ys) / 가동시간(shifts: 오전·오후·연근의
                     시각 범위) / 계기(meter) / 연료·오일·일일점검(글자 필드)

실제에서 본 어려움을 넣는다 (날마다 d % 3 으로 돌아가며):
  · 계기 값은 소수 한 자리(1234.5), 낮은 칸. 같은 장비의 어제 종료 = 오늘 시작 (이어진다)
  · 계기 대신 시각을 적은 장비 (12:00 / 13:00, 점으로 08.00) — 정답은 사람이 입력할 표기(콜론)
  · 계기가 빈 장비 (근무 시각 범위로만 가동 시간을 안다), 그것도 빈 날 (가동 시간 NULL)
  · 하루에 두 장을 낸 장비 (주간·야간 — 묶음 안에서 야간 장이 먼저 나온다: 순서는 계기 시작 값으로 정한다)
  · 며칠 빠진 장비 (계기는 그동안에도 돌았다 → gap 과 사이에 낀 날 수), 시작을 잘못 적은 날 (overlap)
  · 총 ≠ 종료 − 시작 인 쪽, 소계 ≠ 합 인 행
  · 대응표([equipment.aliases])에 없는 장비명
  · 작업량 표 위에 여러 칸에 걸쳐 쓴 메모 (값이 아니다), 대부분 빈 작업량 표
  · 작업량 칸 안에 인쇄된 라벨·단위 (실제의 "하단: _ 대") — 그 구분(PRINTED_ITEMS)의 값은 두 자리. 쓴 숫자가 양옆의 인쇄와,
    인쇄가 이웃 칸의 인쇄와 이어져 덩어리 배정에서는 줄 전체가 메모가 된다
  · 시각 범위를 8-12 처럼 줄여 쓴 칸 (정답은 08:00~12:00)
  · 근무 시각 칸 가운데에 인쇄된 "~" (실제 로우더 작업일보처럼) — 손으로는 그 양옆에 시작·끝을 쓴다 (8-12 는 8 과 12).
    아무것도 쓰지 않은 칸도 인쇄로는 잉크가 있다 (tasks/0006 단계 3)

글씨는 전부 자체 획(tools/handfont.py — 숫자·소수점·콜론·물결표·붙임표·영문 소문자)으로 그린다 — OpenCV 판과 무관하다.
인쇄된 글자(양식)만 OpenCV 내장 글꼴이다 (기준 이미지와 같은 판으로 그리므로 정합에는 상관없다). 근무 시각 칸의 "~" 는
값 유무를 가르는 인쇄라 판과 무관한 선으로 그린다 (_tilde).
난수는 따로 쓴다 (default_rng([seed, 5005])) — 기본 합성 데이터의 난수 흐름을 건드리지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import handfont, synth_cells, synth_meta

T_USAGE, T_LOADER = "synth_usage_log", "synth_loader_log"
PORTRAIT = (1654, 2339)
LANDSCAPE = (2339, 1654)
PRINT = cv2.FONT_HERSHEY_SIMPLEX
DPI = 200

# 장비: (적는 이름, 대응표의 장비 키 — 합성 점검표의 장비 행, 양식, 운전자). 전부 가상의 값
EQUIPMENT = [
    ("LOADER", "EQ-0301", T_LOADER, "ALPHA"),      # 계기가 이어진다, 작업량 표
    ("SHOVEL", "EQ-0401", T_LOADER, "BRAVO"),      # 계기가 비고 근무 시각만 (어떤 날은 그것도 빈다)
    ("TRUCK", "EQ-0501", T_USAGE, "CHARLIE"),      # 하루 두 장, 총이 틀린 날, 시작을 잘못 적은 날
    ("DRILL", "EQ-0201", T_USAGE, "FOXTROT"),      # 빠진 날 (계기는 그동안에도 돌았다)
    ("PUMP", None, T_USAGE, "KILO"),               # 대응표에 없다, 계기 대신 시각
]
NIGHT_OPERATOR = "DELTA"                           # TRUCK 의 야간 장을 쓰는 사람
USAGE_TOML = """
[equipment.aliases]
# 일보에 적는 장비명 → 장비 키 (합성 점검표의 장비 행). 대응을 모르는 이름은 적지 않는다 — 그 쪽의 장비 ID 는 NULL
"LOADER" = "EQ-0301"
"SHOVEL" = "EQ-0401"
"TRUCK" = "EQ-0501"
"DRILL" = "EQ-0201"
"""
ITEMS = ("ORE", "WASTE", "FILL", "SAND", "ROCK", "MUCK", "MISC")
PLACES = ("LOW", "YARD")
PRINTED_ITEMS = (ITEMS[0],)                      # 작업량 칸 안에 라벨(장소의 머리글자와 ":")과 단위("ld")가 인쇄된 구분
SHIFTS = (("am", "AM"), ("pm", "PM"), ("ot", "OT"))
# 작업 표의 낱말·숫자: 세로획만 있는 글씨("1", "drill")는 괘선 지우기(imaging/cells.remove_rules)가 거의 다 지워 잉크 비율로는 빈 칸이
# 된다 (CLAUDE.md "실데이터에서 배운 것"). 판정 규칙은 그대로 두고, 합성 정답이 그 한계를 시험하지 않게 그런 글씨는 쓰지 않는다
WORK_WORDS = ["haul", "load", "scoop", "clean", "move", "wait", "pump", "check", "repair", "level"]
PLACE_WORDS = ["stope", "ramp", "face", "yard", "shaft", "drift", "portal"]


# ── 빈 양식 (이미지 + 템플릿) ──────────────────────────────────────────────
def _canvas(size) -> np.ndarray:
    w, h = size
    return np.full((h, w), 255, np.uint8)


def _label(img, s: str, x: float, y: float, scale: float = 0.8, thick: int = 2) -> None:
    cv2.putText(img, s, (int(x), int(y)), PRINT, scale, 0, thick, cv2.LINE_AA)


def _grid(img, ys, xs, thick: int = 2) -> None:
    for y in ys:
        cv2.line(img, (xs[0], y), (xs[-1], y), 0, thick)
    for x in xs:
        cv2.line(img, (x, ys[0]), (x, ys[-1]), 0, thick)


TILDE_W, TILDE_A, TILDE_T = 32, 5, 3            # 근무 시각 칸에 인쇄된 "~": 폭, 진폭, 굵기 (px)


def _tilde(img, cx: float, cy: float) -> None:
    """인쇄된 물결표 (실제 로우더 작업일보의 근무 시각 칸 "~"). OpenCV 내장 글꼴의 "~" 는 판마다 크기가 크게 다르다
    (배율 1.5·굵기 3 에서 잉크 102 px(5.0) ↔ 276 px(4.9)) — 칸 하나의 값 유무를 가르는 인쇄라 판과 무관한 선으로 그린다.
    스캔한 쪽에서 잉크는 약 300 px (칸의 비율 약 0.014) — 덩어리 배정의 기준 면적(40)과 잉크 비율의 기준(0.008)을 둘 다 넘는다:
    실제처럼 인쇄 층이 없으면 아무것도 쓰지 않은 칸이 "있음"(검수 대기)이다."""
    t = np.linspace(0.0, 2 * np.pi, 25)
    pts = np.stack([cx - TILDE_W / 2 + t / (2 * np.pi) * TILDE_W, cy - TILDE_A * np.sin(t)], 1)
    cv2.polylines(img, [np.round(pts).astype(np.int32)], False, 0, TILDE_T, cv2.LINE_AA)


# 판 B (tasks/0006 4.6, 단계 4): 같은 날 섞여 쓰이는 다른 인쇄 판. 작업 표와 계기 표 두 개만 10 px 아래, 줄 간격 +1 % (정수 좌표).
# 머리 상자·제목·장비명/운전자 필드·서명 상자, 표 아래의 비고 상자·꼬리 문구는 판 A 와 같은 자리 — 표 아래까지 같이 내리면 판 A 만으로도
# 작은 오차로 통과하고 칸도 맞아, 막으려는 실패(판 A 에 정합해 칸이 어긋난 채 적재, 또는 align_failed)가 합성에 없다
T_USAGE_B = "synth_usage_log_b"
B_SHIFT, B_SCALE = 10, 1.01
USAGE_LOGS = (T_USAGE, T_USAGE_B)                 # 운행일보의 판들 (판 B 는 usage_variants 일 때만 사이트 팩에 있다)


def _table_y(top: int, variant: str):
    """표 하나의 y 좌표 변환 (그 표의 위 괘선 기준). 판 A 는 그대로, 판 B 는 B_SHIFT 아래·간격 B_SCALE 배 (반올림한 정수)."""
    if variant == "a":
        return lambda y: y
    return lambda y: top + B_SHIFT + int(round((y - top) * B_SCALE))


def build_usage_log(variant: str = "a") -> tuple[np.ndarray, dict]:
    """variant: "a" (기본 — 지금까지의 판, 바이트까지 그대로) | "b" (판 B: 작업 표·계기 표만 옮긴 판, 이름 T_USAGE_B)."""
    if variant not in ("a", "b"):
        raise ValueError(f"variant 는 a | b: {variant!r}")
    img = _canvas(PORTRAIT)
    _label(img, "EQUIPMENT DAILY OPERATION LOG", 100, 170, 1.4, 3)
    _label(img, "Site: SYNTHETIC MINE  (generated test data - not a real site)", 100, 225, 0.7)
    _label(img, "Equipment:", 100, 335, 0.9)
    _label(img, "Operator:", 830, 335, 0.9)
    sig = (1150, 395, 1554, 475)
    cv2.rectangle(img, sig[:2], sig[2:], 0, 2)
    _label(img, "Signature", 980, 445, 0.8)

    wy = _table_y(520, variant)
    xs = [100, 200, 520, 960, 1180, 1554]                   # No / Location / Work / Hours / Remarks
    ys = [wy(y) for y in [520, 580] + [580 + 70 * (i + 1) for i in range(6)]]
    _grid(img, ys, xs)
    for ci, s in enumerate(("No", "Location", "Work", "Hours", "Remarks")):
        _label(img, s, xs[ci] + 14, ys[0] + 40, 0.7)
    rows = []
    for i in range(6):
        no = "TOTAL" if i == 5 else str(i + 1)
        _label(img, no, xs[0] + 12, ys[i + 1] + 45, 0.55 if i == 5 else 0.7)
        rows.append({"row": i, "key": "total" if i == 5 else f"r{i + 1}", "no": no, **({"subtotal": True} if i == 5 else {})})

    my = _table_y(1050, variant)
    mxs = [100, 400, 818, 1236, 1554]                        # 계기: (인쇄된 이름) / 시작 / 종료 / 총 — 낮은 칸
    mys = [my(y) for y in (1050, 1092, 1138)]
    _label(img, "Hour meter", 100, mys[0] - 15, 0.8)
    _grid(img, mys, mxs)
    for ci, s in enumerate(("", "Start", "End", "Total")):
        if s:
            _label(img, s, mxs[ci] + 14, mys[0] + 30, 0.6)
    _label(img, "Reading", mxs[0] + 14, mys[1] + 32, 0.6)
    _label(img, "Maintenance / remarks:", 100, 1215, 0.8)
    cv2.rectangle(img, (100, 1235), (1554, 1420), 0, 2)
    _label(img, "Write the hour meter with one decimal. If the machine has no meter, write the clock time.", 100, 1480, 0.6)
    _label(img, "Form SYN-USE-01 rev.1", 100, 1520, 0.6)

    spec = {
        "name": T_USAGE if variant == "a" else T_USAGE_B,
        "title": "Equipment daily operation log (synthetic)" + ("" if variant == "a" else " - print variant B"),
        "reference_image": "reference.png",
        "dpi": DPI, "page_size": list(PORTRAIT), "handler": "usage", "handler_options": {},
        "regions": [
            {"name": "work", "role": "activities", "grid": {"ys": ys, "xs": xs}, "header_rows": 1,
             "columns": [{"idx": 0, "name": "no", "kind": "printed"},
                         {"idx": 1, "name": "location", "kind": "handwritten_text"},
                         {"idx": 2, "name": "work", "kind": "handwritten_text"},
                         {"idx": 3, "name": "hours", "kind": "handwritten_text"},
                         {"idx": 4, "name": "remarks", "kind": "handwritten_text"}],
             "rows": rows},
            {"name": "meter", "role": "meter", "grid": {"ys": mys, "xs": mxs}, "header_rows": 1,
             "columns": [{"idx": 1, "name": "start", "kind": "handwritten_number", "format": "reading"},
                         {"idx": 2, "name": "end", "kind": "handwritten_number", "format": "reading"},
                         {"idx": 3, "name": "total", "kind": "handwritten_number", "format": "reading"}],
             "rows": [{"row": 0, "key": "reading"}]},
        ],
        "fields": [
            {"name": "equipment", "kind": "handwritten_text", "bbox": [300, 285, 780, 355], "meta_key": "equipment"},
            {"name": "operator", "kind": "handwritten_text", "bbox": [1010, 285, 1500, 355], "meta_key": "operator"},
            {"name": "signature", "kind": "signature", "bbox": [sig[0] + 6, sig[1] + 6, sig[2] - 6, sig[3] - 6]},
            {"name": "notes", "kind": "handwritten_text", "bbox": [106, 1241, 1548, 1414]},
        ],
    }
    return img, spec


def build_usage_log_b() -> tuple[np.ndarray, dict]:
    return build_usage_log("b")


def concurrent_specs(built: dict) -> None:
    """판 A·B 의 spec 에 같은 family 와 concurrent: true 를 적는다 (usage_variants 일 때만 — 아니면 템플릿은 그대로)."""
    for name in USAGE_LOGS:
        built[name][1].update(family=T_USAGE, concurrent=True)


def assign_variants(plans: list[list], rng) -> None:
    """날마다 운행일보(T_USAGE) 쪽에 판 A·B 를 번갈아 준다 (시작하는 판은 rng 로) — 쪽이 둘 이상인 날은 두 판이 섞인다.
    rng 는 따로 쓴다: 쪽의 내용(계획·글씨)의 난수 흐름은 판을 섞지 않은 것과 같다."""
    for day in plans:
        start = int(rng.integers(2))
        for i, p in enumerate(q for q in day if q.template == T_USAGE):
            if (i + start) % 2:
                p.template = T_USAGE_B


def build_loader_log() -> tuple[np.ndarray, dict]:
    img = _canvas(LANDSCAPE)
    _label(img, "(", 150, 170, 1.5, 3)
    _label(img, ")  LOADER WORK LOG", 640, 170, 1.5, 3)
    _label(img, "Site: SYNTHETIC MINE  (generated test data - not a real site)", 150, 222, 0.7)
    _label(img, "Operator:", 150, 300, 0.9)
    sig = (1800, 240, 2200, 320)
    cv2.rectangle(img, sig[:2], sig[2:], 0, 2)
    _label(img, "Signature", 1630, 290, 0.8)

    # 작업량 표: 구분 7행, 칸마다 하단·저광장 두 줄 (줄 사이에 인쇄된 괘선이 없다 → split_ys)
    xs = [150, 400, 560, 720, 880, 1040]
    sub_h = 44
    ys = [360, 420] + [420 + 2 * sub_h * (i + 1) for i in range(len(ITEMS))]
    splits = [420 + 2 * sub_h * i + sub_h for i in range(len(ITEMS))]
    _label(img, "Work volume (loads)", 150, 345, 0.8)
    _grid(img, ys, xs)
    for ci, s in enumerate(("Item", "Place", "A", "OT", "SUB")):
        _label(img, s, xs[ci] + 14, ys[0] + 40, 0.7)
    rows = []
    for i, item in enumerate(ITEMS):
        _label(img, item, xs[0] + 14, ys[i + 1] + sub_h + 10, 0.7)
        for k, place in enumerate(PLACES):
            _label(img, place, xs[1] + 14, ys[i + 1] + sub_h * k + 30, 0.55)
            if item in PRINTED_ITEMS:                         # 칸 안의 인쇄: "L: __ ld" — 숫자는 그 사이에 쓴다
                for x0, x1 in zip(xs[2:-1], xs[3:], strict=True):
                    _label(img, f"{place[0]}:", x0 + 8, ys[i + 1] + sub_h * k + 30, 0.55)
                    _label(img, "ld", x1 - 30, ys[i + 1] + sub_h * k + 30, 0.55)
            rows.append({"row": 2 * i + k, "key": f"{item}|{place}", "item": item, "place": place})

    sxs = [1200, 1400, 1840]
    sys_ = [360, 420] + [420 + 60 * (i + 1) for i in range(len(SHIFTS))]
    _label(img, "Operating time", 1200, 345, 0.8)
    _grid(img, sys_, sxs)
    _label(img, "Shift", sxs[0] + 14, sys_[0] + 40, 0.7)
    _label(img, "From ~ to", sxs[1] + 14, sys_[0] + 40, 0.7)
    for i, (_k, lab) in enumerate(SHIFTS):
        _label(img, lab, sxs[0] + 14, sys_[i + 1] + 40, 0.7)
        _tilde(img, (sxs[1] + sxs[2]) / 2, (sys_[i + 1] + sys_[i + 2]) / 2)     # 칸 안의 인쇄: "__ ~ __" (tasks/0006 단계 3)

    mxs = [1200, 1570, 1940, 2310]                            # 계기 칸: 긴 값(1234.5)이 이웃 칸의 값과 한 덩어리가 되지 않을 만큼 넓게
    mys = [700, 742, 788]
    _label(img, "Hour meter", 1200, 685, 0.8)
    _grid(img, mys, mxs)
    for ci, s in enumerate(("Start", "End", "Hours")):
        _label(img, s, mxs[ci] + 14, mys[0] + 30, 0.6)

    _label(img, "Fuel (L):", 1200, 900, 0.8)
    _label(img, "Oil (L):", 1700, 900, 0.8)
    _label(img, "Daily check:", 1200, 1000, 0.8)
    _label(img, "Write loads per place. SUB = A + OT.   Form SYN-LOAD-07 rev.1", 150, 1100, 0.6)

    spec = {
        "name": T_LOADER, "title": "Loader work log (synthetic)", "reference_image": "reference.png",
        "dpi": DPI, "page_size": list(LANDSCAPE), "handler": "usage", "handler_options": {},
        "regions": [
            {"name": "tally", "role": "tally", "grid": {"ys": ys, "xs": xs, "split_ys": splits}, "header_rows": 1,
             "columns": [{"idx": 0, "name": "item", "kind": "printed"},
                         {"idx": 1, "name": "place", "kind": "printed"},
                         {"idx": 2, "name": "a", "kind": "handwritten_number", "shift": "A"},
                         {"idx": 3, "name": "ot", "kind": "handwritten_number", "shift": "OT"},
                         {"idx": 4, "name": "sub", "kind": "handwritten_number", "subtotal": True}],
             "rows": rows},
            {"name": "shifts", "role": "shifts", "grid": {"ys": sys_, "xs": sxs}, "header_rows": 1,
             "columns": [{"idx": 0, "name": "shift", "kind": "printed"},
                         {"idx": 1, "name": "range", "kind": "handwritten_number", "format": "time_range"}],
             "rows": [{"row": i, "key": k, "shift": lab} for i, (k, lab) in enumerate(SHIFTS)]},
            {"name": "meter", "role": "meter", "grid": {"ys": mys, "xs": mxs}, "header_rows": 1,
             "columns": [{"idx": 0, "name": "start", "kind": "handwritten_number", "format": "reading"},
                         {"idx": 1, "name": "end", "kind": "handwritten_number", "format": "reading"},
                         {"idx": 2, "name": "total", "kind": "handwritten_number", "format": "reading"}],
             "rows": [{"row": 0, "key": "reading"}]},
        ],
        "fields": [
            {"name": "equipment", "kind": "handwritten_text", "bbox": [190, 105, 620, 185], "meta_key": "equipment"},
            {"name": "operator", "kind": "handwritten_text", "bbox": [330, 250, 800, 315], "meta_key": "operator"},
            {"name": "signature", "kind": "signature", "bbox": [sig[0] + 6, sig[1] + 6, sig[2] - 6, sig[3] - 6]},
            {"name": "fuel", "kind": "handwritten_text", "bbox": [1380, 855, 1680, 915]},
            {"name": "oil", "kind": "handwritten_text", "bbox": [1860, 855, 2160, 915]},
            {"name": "check", "kind": "handwritten_text", "bbox": [1430, 955, 2160, 1015]},
        ],
    }
    return img, spec


BUILDERS = {T_USAGE: build_usage_log, T_LOADER: build_loader_log}
VARIANT_BUILDERS = {T_USAGE_B: build_usage_log_b}           # usage_variants 일 때 더한다 (tasks/0006 단계 4)


# ── 계획: 날마다 쪽마다 무엇을 적는가 (정답) ─────────────────────────────────
@dataclass
class PagePlan:
    template: str
    equipment: str
    alias_key: str | None
    operator: str
    meter: dict = field(default_factory=dict)        # 칸 → 종이에 적은 글자 ("1234.5", "08.00", …)
    truth_meter: dict = field(default_factory=dict)  # 칸 → 정답 (사람이 입력할 정규화한 표기)
    shifts: dict = field(default_factory=dict)       # 행 키 → (종이의 글자, 정답)
    tally: dict = field(default_factory=dict)        # (행 키, 열) → 수
    memo_rows: tuple = ()                            # 작업량 표 위에 메모를 쓴 행 (값이 없는 행)
    activities: list = field(default_factory=list)   # 줄마다 {열: 낱말}
    texts: dict = field(default_factory=dict)        # 글자 필드 → 값
    signed: bool = True
    order: int = 0                                   # 묶음 안의 순서 (작을수록 앞)
    scenarios: list = field(default_factory=list)


def _tenths(rng, lo: float, hi: float) -> float:
    return round(float(rng.integers(int(lo * 10), int(hi * 10) + 1)) / 10, 1)


def _fmt(v: float) -> str:
    return f"{v:.1f}"


def _clock(h: int, m: int = 0) -> str:
    return f"{h:02d}:{m:02d}"


def plan_days(days: list[str], rng) -> list[list[PagePlan]]:
    """날짜마다 쪽 계획. 계기 값은 장비마다 달리는 시간계 — 빠진 날에도 돈다."""
    meters = {name: _tenths(rng, 1000, 4000) for name, *_ in EQUIPMENT}
    out = []
    for d, _day in enumerate(days):
        pages: list[PagePlan] = []
        for k, (name, key, tpl, op) in enumerate(EQUIPMENT):
            used = _tenths(rng, 5.0, 9.5)
            start, end = meters[name], round(meters[name] + used, 1)
            meters[name] = end
            if name == "DRILL" and d % 3 == 1:                  # 빠진 날 — 시간계는 돌았다
                continue
            p = PagePlan(tpl, name, key, op, order=10 * k)
            if name == "LOADER":
                _set_meter(p, start, end, total=round(end - start, 1) if d % 3 == 0 else None)
                _set_shifts(p, rng, d)
                _set_tally(p, rng, d)
            elif name == "SHOVEL":
                if d % 3 == 1:
                    p.scenarios.append("SHOVEL_nothing_written")
                else:
                    _set_shifts(p, rng, d)
                    p.scenarios.append("SHOVEL_shifts_only")
            elif name == "TRUCK" and d % 3 == 0:                # 하루 두 장 (주간·야간). 야간 장이 묶음에서 먼저 나온다
                mid = round(start + _tenths(rng, 2.0, used - 2.0), 1)
                _set_meter(p, start, mid)
                night = PagePlan(tpl, name, key, NIGHT_OPERATOR, order=10 * k - 1, scenarios=["TRUCK_two_sheets"])
                _set_meter(night, mid, end)
                _set_activities(night, rng)
                pages.append(night)
                p.scenarios.append("TRUCK_two_sheets")
            elif name == "TRUCK" and d % 3 == 1:                # 총을 적었는데 종료 − 시작과 다르다
                _set_meter(p, start, end, total=round(end - start + 1.0, 1))
                p.scenarios.append("TRUCK_total_mismatch")
            elif name == "TRUCK":                               # 시작을 잘못 적었다 (어제의 종료보다 2.0 작게) → overlap
                _set_meter(p, round(start - 2.0, 1), end)
                p.scenarios.append("TRUCK_wrong_start")
            elif name == "DRILL":
                _set_meter(p, start, end)
                if d % 3 == 2:
                    p.scenarios.append("DRILL_after_missing_day")
            elif name == "PUMP":                                # 계기 대신 시각. 점으로 쓴 시각(08.00)도 정답은 콜론
                h0 = int(rng.integers(6, 10))
                h1 = h0 + int(rng.integers(6, 10))
                a = _clock(h0) if rng.random() < 0.5 else f"{h0:02d}.00"
                b = f"{h1:02d}.00" if d % 3 == 0 else _clock(h1)
                p.meter, p.truth_meter = {"start": a, "end": b}, {"start": _clock(h0), "end": _clock(h1)}
                p.signed = d % 3 != 1
                p.scenarios.append("PUMP_clock_unaliased")
            if tpl == T_USAGE:
                _set_activities(p, rng)
                if rng.random() < 0.5:
                    p.texts["notes"] = " ".join(str(rng.choice(WORK_WORDS)) for _ in range(2))
            else:
                p.texts.update(fuel=str(int(rng.integers(40, 160))), oil=str(int(rng.integers(1, 9))), check="ok")
            pages.append(p)
        pages.sort(key=lambda p: p.order)
        out.append(pages)
    return out


def _set_meter(p: PagePlan, start: float, end: float, total: float | None = None) -> None:
    p.meter = {"start": _fmt(start), "end": _fmt(end)}
    if total is not None:
        p.meter["total"] = _fmt(total)
    p.truth_meter = dict(p.meter)


def _set_shifts(p: PagePlan, rng, d: int) -> None:
    am0 = int(rng.integers(6, 9))
    am1, pm0 = am0 + 4, am0 + 5
    pm1 = pm0 + int(rng.integers(3, 5))
    p.shifts["am"] = (f"{am0}-{am1}" if d % 3 == 2 else f"{_clock(am0)}~{_clock(am1)}", f"{_clock(am0)}~{_clock(am1)}")
    p.shifts["pm"] = (f"{_clock(pm0)}~{_clock(pm1)}", f"{_clock(pm0)}~{_clock(pm1)}")
    if rng.random() < 0.5:                                      # 연근: 끝이 자정을 넘는 날도 있다
        o0 = pm1 + 1
        o1 = (o0 + int(rng.integers(2, 5))) % 24
        p.shifts["ot"] = (f"{_clock(o0)}~{_clock(o1)}", f"{_clock(o0)}~{_clock(o1)}")


def _set_tally(p: PagePlan, rng, d: int) -> None:
    """대부분 빈 작업량 표. 값은 앞의 두 구분에만. d % 3 == 1 이면 뒤쪽 빈 행 위에 메모, == 2 면 소계 하나가 틀렸다."""
    if d % 3 == 1:
        p.memo_rows = (f"{ITEMS[4]}|{PLACES[1]}",)
        p.scenarios.append("LOADER_memo_on_tally")
    for item in ITEMS[:2]:
        for place in PLACES:
            if rng.random() < 0.4:
                continue
            a = int(rng.integers(1, 15)) + (10 if item in PRINTED_ITEMS else 0)   # 인쇄 사이의 값은 두 자리 (난수 흐름은 같다)
            ot = int(rng.integers(1, 6)) if rng.random() < 0.4 else None
            rk = f"{item}|{place}"
            p.tally[(rk, "a")] = a
            if ot is not None:
                p.tally[(rk, "ot")] = ot
            p.tally[(rk, "sub")] = a + (ot or 0)
    if d % 3 == 2 and p.tally:
        rk = sorted({k[0] for k in p.tally})[0]
        p.tally[(rk, "sub")] += 1
        p.scenarios.append("LOADER_subtotal_mismatch")


def _set_activities(p: PagePlan, rng) -> None:
    for _ in range(int(rng.integers(1, 5))):
        p.activities.append({"location": str(rng.choice(PLACE_WORDS)), "work": str(rng.choice(WORK_WORDS)),
                             "hours": str(int(rng.integers(2, 7)))})


# ── 그리기 ─────────────────────────────────────────────────────────────────
def _ink_extent(g: np.ndarray) -> float:
    """글자의 폭: 잉크 질량이 열 방향으로 1 %–99 % 인 구간. 문턱(0.2)으로 자른 폭은 OpenCV 판마다 안티에일리어싱이 조금 달라
    1 px 씩 달라졌고(붙임표 22 ↔ 23), 그 뒤의 글자가 다 밀렸다 — 질량으로 재면 그 차이가 소수점 아래로 줄어든다."""
    col = g.sum(axis=0).astype(np.float64)
    if col.sum() <= 0:
        return g.shape[1] * 0.5
    c = np.cumsum(col) / col.sum()
    return float(np.interp(0.99, c, np.arange(len(c))) - np.interp(0.01, c, np.arange(len(c)))) + 1.0


def _text_ink(text: str, h: float, style: dict, rng) -> tuple[np.ndarray, float, float]:
    """숫자·기호 한 줄의 잉크 (0–1). 돌려주는 값: (그림, 글씨가 시작하는 x, 글씨 폭). 세로 가운데 = 그림 높이의 반.
    그림의 크기는 글자 수와 높이로만 정한다 (잰 폭으로 정하면 판마다 1 px 달라질 수 있다)."""
    glyphs = [synth_cells._glyph(ch, h * (float(rng.uniform(0.92, 1.06)) if ch.isdigit() else 1.0), style, rng)
              for ch in text]
    widths = [_ink_extent(g) for g in glyphs]
    gap = h * float(rng.uniform(0.05, 0.12))
    total = sum(widths) + gap * (len(glyphs) - 1)
    m = h * 0.8
    ink = np.zeros((int(h * 2.4), int(len(text) * h * 1.1 + 2 * m)), np.float32)
    x, cy = m, ink.shape[0] / 2
    for ch, g, w in zip(text, glyphs, widths, strict=True):
        dy = handfont.glyph_offset(ch) * h
        synth_cells._paste(ink, g, x + w / 2, cy + dy + float(rng.uniform(-0.04, 0.04)) * h,
                           style["ink"] * float(rng.uniform(0.9, 1.0)))
        x += w + gap
    return ink, m, total


def _composite(img, ink, ox: float, oy: float, rng) -> None:
    from .synth import _composite as comp

    comp(img, ink, int(round(ox)), int(round(oy)), int(rng.integers(20, 70)))


def _write_in_cell(img, text: str, bbox, rng, style, size: tuple[float, float]) -> None:
    """칸 가운데 근처에 숫자·기호를 쓴다. size: 글자 높이 / 괘선 사이 높이 (구간)."""
    x0, y0, x1, y1 = bbox
    gh = (y1 - y0) + 8                                          # bbox 는 괘선 안쪽 4 px — 괘선 사이 높이로
    h = gh * float(rng.uniform(*size))
    ink, m, total = _text_ink(text, h, style, rng)
    cx = (x0 + x1) / 2 + float(rng.uniform(-0.08, 0.08)) * max(0.0, (x1 - x0) - total)
    cy = (y0 + y1) / 2 + float(rng.uniform(-1.5, 1.5))
    _composite(img, ink, cx - m - total / 2, cy - ink.shape[0] / 2, rng)


def _words(img, words: str, x: float, cy: float, h: float, rng, style) -> None:
    W = int(len(words) * h * 0.9 + 4 * h)
    ink = np.zeros((int(h * 2.6), W), np.float32)
    synth_cells._draw_text_line(ink, words, h * 0.5, ink.shape[0] / 2, h, rng, style)
    _composite(img, ink, x - h * 0.5, cy - ink.shape[0] / 2, rng)


def _signature(img, box, rng) -> None:
    x0, y0, x1, y1 = box
    n = int(rng.integers(6, 11))
    xs = np.linspace(x0 + 30, x0 + 30 + (x1 - x0) * float(rng.uniform(0.4, 0.7)), n)
    ys = (y0 + y1) / 2 + rng.uniform(-0.3, 0.3, n) * (y1 - y0)
    pts = np.stack([xs, ys], 1).astype(np.int32)
    cv2.polylines(img, [pts], False, int(rng.integers(20, 70)), 3, cv2.LINE_AA)


def _cell_box(reg: dict, row: int, col_name: str, inset: int = 4) -> tuple[int, int, int, int]:
    from ..forms.template import cell_lines

    ys, xs = cell_lines(reg)
    c = next(c for c in reg["columns"] if c["name"] == col_name)
    gi = row + reg.get("header_rows", 0)
    return xs[c["idx"]] + inset, ys[gi] + inset, xs[c["idx"] + 1] - inset, ys[gi + 1] - inset


def fill_page(blank: np.ndarray, spec: dict, p: PagePlan, rng) -> np.ndarray:
    from .synth import _meta_field

    img = blank.copy()
    style = synth_meta.page_style(synth_meta.writer_style(p.operator), rng)
    fields = {f["name"]: f for f in spec["fields"]}
    regs = {r["name"]: r for r in spec["regions"]}
    _meta_field(img, tuple(fields["equipment"]["bbox"]), p.equipment, "name", style, rng)
    _meta_field(img, tuple(fields["operator"]["bbox"]), p.operator, "name", style, rng)
    if p.signed:
        _signature(img, fields["signature"]["bbox"], rng)
    # 낮은 계기 칸. 덩어리 배정(imaging/blobs.py)은 칸 폭의 절반까지 가로로 붙여 묶는다 — 이웃 칸의 값과 그만큼 떨어지게 쓴다
    for slot, text in p.meter.items():
        _write_in_cell(img, text, _cell_box(regs["meter"], 0, slot), rng, style, (0.6, 0.75))
    if "shifts" in regs:
        rows = {r["key"]: r["row"] for r in regs["shifts"]["rows"]}
        for k, (text, _truth) in p.shifts.items():                 # 인쇄된 "~" 의 양옆에: 시작은 왼쪽, 끝은 오른쪽 (8-12 → 8, 12)
            x0, y0, x1, y1 = _cell_box(regs["shifts"], rows[k], "range")
            cx, gap = (x0 + x1) / 2, TILDE_W / 2 + 14
            a, b = re.split("[~-]", text)
            _write_in_cell(img, a, (x0, y0, int(cx - gap), y1), rng, style, (0.5, 0.65))
            _write_in_cell(img, b, (int(cx + gap), y0, x1, y1), rng, style, (0.5, 0.65))
    if "tally" in regs:
        reg = regs["tally"]
        rows = {r["key"]: r["row"] for r in reg["rows"]}
        for (rk, col), v in p.tally.items():                    # 나눔 선 사이(높이 44)에서 위아래 숫자가 닿지 않게
            _write_in_cell(img, str(v), _cell_box(reg, rows[rk], col), rng, style, (0.55, 0.7))
        for rk in p.memo_rows:                                  # 값이 없는 행 위에 여러 칸에 걸쳐 쓴 메모
            x0, y0, _x1, y1 = _cell_box(reg, rows[rk], "a")
            _words(img, "stopped for water in drift", x0 + 10, (y0 + y1) / 2, 32, rng, style)
    if "work" in regs:
        reg = regs["work"]
        for i, act in enumerate(p.activities):
            for col, text in act.items():
                x0, y0, x1, y1 = _cell_box(reg, i, col)
                if text.isdigit():
                    _write_in_cell(img, text, (x0, y0, x1, y1), rng, style, (0.5, 0.6))
                else:
                    _words(img, text, x0 + 16, (y0 + y1) / 2, 34, rng, style)
    for name, text in p.texts.items():
        x0, y0, x1, y1 = fields[name]["bbox"]
        if text.isdigit():
            _write_in_cell(img, text, (x0, y0, x1, y1), rng, style, (0.55, 0.7))
        else:
            _words(img, text, x0 + 20, (y0 + y1) / 2 if y1 - y0 < 100 else y0 + 50, 34, rng, style)
    return img


# ── 정답 ───────────────────────────────────────────────────────────────────
def answers_of(source: str, p: PagePlan) -> list[dict]:
    """eval·oracle 형식의 정답 (값이 있는 칸만): 종이에 적힌 것을 사람이 입력할 표기로."""
    tpl = p.template

    def a(region, name, row_key, text):
        return {"source": source, "template": tpl, "region": region, "field_name": name, "row_key": row_key, "text": text}

    out = [a("fields", "equipment", "", p.equipment), a("fields", "operator", "", p.operator)]
    out += [a("meter", slot, "reading", v) for slot, v in p.truth_meter.items()]
    out += [a("shifts", "range", k, truth) for k, (_text, truth) in p.shifts.items()]
    out += [a("tally", col, rk, str(v)) for (rk, col), v in sorted(p.tally.items())]
    for i, act in enumerate(p.activities):
        out += [a("work", col, f"r{i + 1}", text) for col, text in act.items()]
    out += [a("fields", name, "", text) for name, text in p.texts.items()]
    return out


def truth_of(source: str, day: str, p: PagePlan) -> dict:
    """그 쪽의 eq_usage_daily 정답 (검수로 정답을 전부 넣었을 때) — handlers/usage.py 와 다른 코드로 계산한다."""
    tm = p.truth_meter
    clock = any(":" in v for v in tm.values())
    num = {k: float(v) for k, v in tm.items() if ":" not in v}
    rec = {"source": source, "date": day, "template": p.template, "equipment": p.equipment, "alias_key": p.alias_key,
           "operator": p.operator, "meter_start": num.get("start"), "meter_end": num.get("end"),
           "meter_total": num.get("total"), "clock_start": tm.get("start") if clock else None,
           "clock_end": tm.get("end") if clock else None,
           "reading_kind": "clock" if clock else ("meter" if num else "empty"),
           "shifts": {k: truth for k, (_t, truth) in p.shifts.items()} if p.template == T_LOADER else None,
           "activity_rows": len(p.activities) if p.template != T_LOADER else None, "signed": int(p.signed),
           "tally": [{"row_key": rk, "column": col, "count": v} for (rk, col), v in sorted(p.tally.items())],
           "scenarios": p.scenarios}
    mins = None
    if p.shifts:
        mins = 0
        for _t, truth in p.shifts.values():
            a, b = truth.split("~")
            m0, m1 = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
            mins += m1 - m0 if m1 >= m0 else m1 + 1440 - m0
    rec["shift_minutes"] = mins
    if "start" in num and "end" in num:
        rec["hours"], rec["hours_basis"] = round(num["end"] - num["start"], 4), "meter"
    elif "total" in num:
        rec["hours"], rec["hours_basis"] = num["total"], "total"
    elif clock:
        a, b = tm["start"], tm["end"]
        m0, m1 = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
        rec["hours"], rec["hours_basis"] = round((m1 - m0 if m1 >= m0 else m1 + 1440 - m0) / 60, 4), "clock"
    elif mins is not None:
        rec["hours"], rec["hours_basis"] = round(mins / 60, 4), "shifts"
    else:
        rec["hours"], rec["hours_basis"] = None, None
    return rec
