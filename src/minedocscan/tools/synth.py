"""합성 양식 생성기 — 개인정보 없는 시험용 사이트 팩과 스캔 문서를 만든다.

실제 현장 문서에는 이름·서명·차량번호가 있어 저장소에 넣지 않는다. 대신 이 모듈이 만드는 가상의
양식 세 종으로 파이프라인 전체를 시험한다. 무엇을 적었는지(정답)를 알고 있으므로 인식 모델 없이도
분류·정합·셀 추출·체크 판정·교차검증·적재가 맞는지 수치로 확인할 수 있다.

  synth_inspection   세로 — 장비 점검표 (인쇄된 장비 목록, 수기 점검내역, 이상 유/무 체크)
  synth_haul_log     가로 — 차량별 운반 일보 (광종×편 행, 주간/야간 횟수, 옆에 운행 기록 표)
  synth_haul_matrix  가로 — 편×차량 운반 행렬 (같은 값이 한 번 더 적힌다 → 교차검증)

실제 데이터에서 겪은 어려움을 일부러 넣는다.
  · 스캔 효과 (회전·이동·배율·흐림·잡음·JPEG 압축)
  · ✓ 가 왼쪽 칸에서 시작해 경계선을 넘어 오른쪽 칸까지 그려진다
  · 점검을 하지 않은 날 (체크 열 전체가 빈다)
  · 행렬 양식에 인쇄된 머리글(운전자·차량)과 그날의 실제 배차가 다르다
  · 일보를 내지 않은 차량
  · 표 위에 여러 칸에 걸쳐 쓴 메모
  · 두 양식의 값이 서로 다른 칸 (= 교차검증이 잡아야 하는 것)

선택 기능 low_cells=True (tasks/0003 단계 3): 운반 양식 두 종의 칸이 실제처럼 낮고(행 높이 30 px) 숫자가 거칠고 칸보다 커서
위아래 괘선을 넘으며, 빈 칸 몇 개에 X 표가 있다. X 표 칸은 잉크로는 "값 있음"이지만 정답은 빈 칸이다.
기본 합성 데이터(low_cells=False)는 그대로다 — 기존 테스트의 기대 수치를 바꾸지 않는다.

선택 기능 meta_fields=True (tasks/0004 단계 2): 일보의 차량번호가 네 자리 숫자, 작성자는 사람마다 다른 획(tools/synth_meta.py),
날짜 줄에 월·일 필드(meta_key date.month, date.day). 날마다 돌아가며: 두 사람이 차를 바꿔 탄 날, 처음 보는 차, 처음 보는
사람이 쓴 날. mix_pages=True 면 마지막 날의 묶음에 첫날의 일보 한 쪽이 섞인다 (적힌 날짜가 파일의 날짜와 다르다).
메타 필드 합성에는 점검표가 없다 (일보와 행렬만 — 그 시험에 쓰지 않는 쪽을 돌리지 않게).

글자는 OpenCV 내장 글꼴(영문)로 그린다 — 글꼴 파일에 의존하지 않기 위해서다. 따라서 이 데이터로
잴 수 있는 것은 파이프라인의 기하·논리이지 한글 손글씨 인식률이 아니다 (docs/DATA.md).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import cv2
import numpy as np
import yaml

from ..imaging.io import imwrite
from . import synth_cells, synth_meta

DPI = 200
PORTRAIT = (1654, 2339)          # (폭, 높이) — A4 @ 200 dpi
LANDSCAPE = (2339, 1654)
PRINT = cv2.FONT_HERSHEY_SIMPLEX
HAND = cv2.FONT_HERSHEY_SCRIPT_SIMPLEX

T_INSP, T_LOG, T_MATRIX = "synth_inspection", "synth_haul_log", "synth_haul_matrix"

# (구분, 형식, 등록번호) — 전부 가상의 값. 마지막 행은 양식의 여백 행
EQUIPMENT = [
    ("Charger", "CH-200", "EQ-0101"),
    ("Drill", "DR-310", "EQ-0201"),
    ("Drill", "DR-310", "EQ-0202"),
    ("Loader", "LD-450", "EQ-0301"),
    ("Loader", "LD-450", "EQ-0302"),
    ("Loader", "LD-600", "EQ-0303"),
    ("Excavator", "EX-140", "EQ-0401"),
    ("Truck", "DT-15", "EQ-0501"),
    ("Truck", "DT-15", "EQ-0502"),
    ("Truck", "DT-15", "EQ-0503"),
    ("Truck", "DT-15", "EQ-0504"),
    ("LightVehicle", "LV-1", "EQ-0601"),
    ("", "", "-"),
]
MATERIALS = ("ORE", "WASTE")
LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")
UG_ROWS = [(m, lv) for m in MATERIALS for lv in LEVELS]          # 행렬과 일보가 공유하는 행
SURFACE_ROW = ("SURFACE", "-")                                   # 일보에만 있는 행 → 교차검증에서 제외
NOTE_ROW = UG_ROWS[-1]                                           # 메모를 쓰는 행 (값은 적지 않는다)
# 행렬 양식에 인쇄된 머리글: 자리, 차량번호, 운전자
SLOTS = [("T01", "V-101", "ALPHA"), ("T02", "V-102", "BRAVO"), ("T03", "V-103", "CHARLIE"), ("T04", "V-104", "DELTA")]
# 개정판(선택): 둘째 날부터 T02 자리의 인쇄된 차량·운전자가 바뀐 판. 머리글 몇 글자만 달라 모양으로는 가릴 수 없다
SLOTS_V2 = [("T01", "V-101", "ALPHA"), ("T02", "V-202", "GOLF"), ("T03", "V-103", "CHARLIE"), ("T04", "V-104", "DELTA")]
T_MATRIX_V2 = "synth_haul_matrix_v2"
# 메타 필드 (선택): 차량번호가 네 자리 숫자인 머리글. 앞 두 자리가 같은 번호가 여럿 (tools/synth_meta.py 의 VEHICLES)
SLOTS_META = [("T01", "4127", "ALPHA"), ("T02", "4183", "BRAVO"), ("T03", "4135", "CHARLIE"), ("T04", "5260", "DELTA")]
# 메타 필드 일보의 머리 (템플릿 px): 인쇄된 글자와 필드 사이를 띄운다 — 크롭(여유 8 px)에 OpenCV 내장 글꼴의 글자가 들어가지 않게
META_BOXES = {"date_month": (530, 205, 670, 270), "date_day": (775, 205, 915, 270),
              "vehicle_no": (340, 290, 730, 355), "operator": (920, 290, 1290, 355)}
MATRIX_FAMILY = "synth_haul_matrix"
# 낮은 칸 (선택): 실제 운반 칸처럼 행 높이 30 px. 숫자 칸 폭은 덩어리 배정(imaging/blobs.py)이 가로로 붙여 묶는 거리(칸 폭의
# 절반)보다 이웃 칸 숫자 사이가 넓게 남도록 120 px — 판정 규칙은 건드리지 않는다
LOW_ROW_H, LOW_NUM_W = 30, 120
OK_PHRASES = ["ok", "greased", "filter cleaned", "washed", "checked"]
FAULT_PHRASES = ["oil leak", "tire worn", "hose broken", "lamp broken", "brake noise", "battery low"]
SIDE_LABELS = ["Fuel (L)", "Engine hours", "Odometer (km)", "Start time", "End time"]


# ── 그리기 도우미 ──────────────────────────────────────────────────────────
def _canvas(size: tuple[int, int]) -> np.ndarray:
    w, h = size
    return np.full((h, w), 255, np.uint8)


def _label(img, s: str, x: float, y: float, scale: float = 0.8, thick: int = 2) -> None:
    cv2.putText(img, s, (int(x), int(y)), PRINT, scale, 0, thick, cv2.LINE_AA)


def _grid(img, ys: list[int], xs: list[int], thick: int = 2) -> None:
    for y in ys:
        cv2.line(img, (xs[0], y), (xs[-1], y), 0, thick)
    for x in xs:
        cv2.line(img, (x, ys[0]), (x, ys[-1]), 0, thick)


def _cell_label(img, s: str, ys, xs, ri: int, ci: int, scale: float = 0.7) -> None:
    if s:
        _label(img, s, xs[ci] + 14, (ys[ri] + ys[ri + 1]) // 2 + 9, scale)


def _cell_bbox(ys, xs, ri: int, ci: int) -> tuple[int, int, int, int]:
    return xs[ci], ys[ri], xs[ci + 1], ys[ri + 1]


def _hand(img, text: str, x: float, y: float, rng, scale: float = 1.2, thick: int = 2) -> None:
    """손글씨 흉내: 필기체 글꼴 + 위치·기울기 흔들림. (x, y) 는 글씨의 왼쪽 아래."""
    (tw, th), base = cv2.getTextSize(text, HAND, scale, thick)
    pad = 12
    patch = np.full((th + base + 2 * pad, tw + 2 * pad), 255, np.uint8)
    cv2.putText(patch, text, (pad, pad + th), HAND, scale, int(rng.integers(20, 70)), thick, cv2.LINE_AA)
    m = cv2.getRotationMatrix2D((patch.shape[1] / 2, patch.shape[0] / 2), float(rng.uniform(-4, 4)), 1.0)
    patch = cv2.warpAffine(patch, m, (patch.shape[1], patch.shape[0]), borderValue=255)
    x0 = int(x + rng.integers(-4, 5)) - pad
    y0 = int(y + rng.integers(-4, 5)) - th - pad
    h, w = img.shape
    x1, y1 = min(w, x0 + patch.shape[1]), min(h, y0 + patch.shape[0])
    x0c, y0c = max(0, x0), max(0, y0)
    if x1 > x0c and y1 > y0c:
        sub = patch[y0c - y0:y1 - y0, x0c - x0:x1 - x0]
        img[y0c:y1, x0c:x1] = np.minimum(img[y0c:y1, x0c:x1], sub)


def _hand_in_cell(img, text: str, bbox, rng, scale: float = 1.2, thick: int = 2, center: bool = False) -> None:
    x0, y0, x1, y1 = bbox
    (tw, th), _ = cv2.getTextSize(text, HAND, scale, thick)
    x = (x0 + x1 - tw) / 2 if center else x0 + 18
    _hand(img, text, x, (y0 + y1 + th) / 2, rng, scale, thick)


def _tick(img, bbox, rng, cross_right: bool = False) -> None:
    """✓ 를 그린다. cross_right 면 꼬리가 오른쪽 칸까지 넘어간다 (현장에서 흔한 모양)."""
    x0, y0, x1, y1 = bbox
    cx = x0 + (x1 - x0) * 0.45 + rng.uniform(-5, 5)
    cy = (y0 + y1) / 2 + rng.uniform(-5, 5)
    tail = (x1 - x0) * 0.7 if cross_right else 22
    pts = np.array([[cx - 16, cy - 4], [cx - 4, cy + 14], [cx + tail, cy - 20]], np.int32)
    cv2.polylines(img, [pts], False, int(rng.integers(20, 70)), 3, cv2.LINE_AA)


def scan_effect(img: np.ndarray, rng, strength: float = 1.0) -> np.ndarray:
    """스캐너를 흉내 낸다: 약간 돌고, 밀리고, 흐려지고, 종이색과 잡음이 낀다."""
    h, w = img.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), float(rng.uniform(-1.0, 1.0)) * strength,
                                1.0 + float(rng.uniform(-0.012, 0.012)) * strength)
    m[:, 2] += rng.uniform(-14, 14, 2) * strength
    out = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)
    out = cv2.GaussianBlur(out, (3, 3), 0).astype(np.float32)
    out = out * (float(rng.uniform(232, 250)) / 255.0) + rng.normal(0, 4.0 * strength, out.shape).astype(np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


# ── 빈 양식 세 종 (이미지 + 템플릿 정의) ───────────────────────────────────
def build_inspection() -> tuple[np.ndarray, dict]:
    img = _canvas(PORTRAIT)
    _label(img, "DAILY EQUIPMENT INSPECTION SHEET", 100, 190, 1.5, 3)
    _label(img, "Site: SYNTHETIC MINE  (generated test data - not a real site)", 100, 245, 0.7)
    _label(img, "Date:  20      .        .          (        )", 100, 340, 0.9)
    ax, ay = [1110, 1260, 1410, 1560], [230, 280, 360]
    _grid(img, ay, ax)
    for i, s in enumerate(("Prepared", "Checked", "Approved")):
        _label(img, s, ax[i] + 14, ay[0] + 34, 0.6)

    xs = [100, 330, 560, 800, 1360, 1460, 1560]
    ys = [400, 480] + [480 + 100 * (i + 1) for i in range(len(EQUIPMENT))]
    _grid(img, ys, xs)
    for ci, s in enumerate(("Category", "Model", "Reg. No", "Inspection notes", "Abn Y", "Abn N")):
        _cell_label(img, s, ys, xs, 0, ci)
    rows = []
    for i, (cat, model, reg) in enumerate(EQUIPMENT):
        for ci, s in enumerate((cat, model, reg)):
            _cell_label(img, s, ys, xs, i + 1, ci)
        rows.append({"row": i, "key": reg if cat else "", "category": cat, "model": model, "registration": reg})
    _label(img, "Mark Y when any abnormality is found and describe it in the notes column.", 100, ys[-1] + 60, 0.7)
    _label(img, "Keep this sheet for one year.   Form SYN-INSP-01 rev.2", 100, ys[-1] + 100, 0.7)

    spec = {
        "name": T_INSP, "title": "Daily equipment inspection (synthetic)", "reference_image": "reference.png",
        "dpi": DPI, "page_size": list(PORTRAIT), "handler": "inspection",
        "handler_options": {"region": "main", "yes_column": "abnormal_yes", "no_column": "abnormal_no",
                            "text_column": "remark"},
        "regions": [{
            "name": "main", "grid": {"ys": ys, "xs": xs}, "header_rows": 1,
            "columns": [
                {"idx": 0, "name": "category", "kind": "printed"},
                {"idx": 1, "name": "model", "kind": "printed"},
                {"idx": 2, "name": "registration", "kind": "printed"},
                {"idx": 3, "name": "remark", "kind": "handwritten_text"},
                {"idx": 4, "name": "abnormal_yes", "kind": "checkmark"},
                {"idx": 5, "name": "abnormal_no", "kind": "checkmark"},
            ],
            "rows": rows,
        }],
        "fields": [
            {"name": "date_line", "kind": "handwritten_text", "bbox": [190, 295, 900, 360]},
            {"name": "approval_box", "kind": "signature", "bbox": [1114, 284, 1556, 356]},
        ],
    }
    return img, spec


def _haul_rows(rows: list[tuple[str, str]]) -> list[dict]:
    return [{"row": i, "key": f"{m}|{lv}", "material": m, "level": lv} for i, (m, lv) in enumerate(rows)]


def build_haul_log(low: bool = False, meta: bool = False) -> tuple[np.ndarray, dict]:
    img = _canvas(LANDSCAPE)
    _label(img, "DUMP TRUCK DAILY HAUL LOG", 150, 170, 1.5, 3)
    if meta:                       # 월·일 칸이 따로 있는 날짜 줄 (인쇄된 "Month"·"Day" 와 밑줄)
        _label(img, "Date:  20", 150, 255, 0.9)
        _label(img, "Month", 420, 255, 0.9)
        _label(img, "Day", 700, 255, 0.9)
        for name in ("date_month", "date_day"):
            x0, _y0, x1, y1 = META_BOXES[name]
            cv2.line(img, (x0, y1 - 6), (x1, y1 - 6), 0, 2)
    else:
        _label(img, "Date:  20      .        .", 150, 255, 0.9)
    _label(img, "Vehicle No:", 150, 335, 0.9)
    _label(img, "Operator:", 760, 335, 0.9)
    rows = UG_ROWS + [SURFACE_ROW]
    if low:
        xs = [150, 400, 600, 600 + LOW_NUM_W, 600 + 2 * LOW_NUM_W]
        ys = [380, 430] + [430 + LOW_ROW_H * (i + 1) for i in range(len(rows))]
    else:
        xs = [150, 400, 600, 850, 1100]
        ys = [380, 450] + [450 + 80 * (i + 1) for i in range(len(rows))]
    _grid(img, ys, xs)
    for ci, s in enumerate(("Material", "Level", "Day", "Night") if low else ("Material", "Level", "Day trips", "Night trips")):
        _cell_label(img, s, ys, xs, 0, ci, 0.6 if low else 0.7)
    for i, (m, lv) in enumerate(rows):
        _cell_label(img, m, ys, xs, i + 1, 0, 0.55 if low else 0.7)
        _cell_label(img, lv, ys, xs, i + 1, 1, 0.55 if low else 0.7)

    sxs = [1300, 1650, 2150]
    sys_ = [450 + 90 * i for i in range(len(SIDE_LABELS) + 1)]
    _label(img, "Operating record", 1300, 425, 0.9)
    _grid(img, sys_, sxs)
    for i, s in enumerate(SIDE_LABELS):
        _cell_label(img, s, sys_, sxs, i, 0)
    _label(img, "One sheet per vehicle per day. Write the number of trips in each cell.", 1300, 1020, 0.7)
    _label(img, "Form SYN-HAUL-01 rev.3", 1300, 1060, 0.7)

    spec = {
        "name": T_LOG, "title": "Dump truck daily haul log (synthetic)", "reference_image": "reference.png",
        "dpi": DPI, "page_size": list(LANDSCAPE), "handler": "haul",
        "handler_options": {"role": "log", "region": "haul"},
        "regions": [
            {"name": "haul", "grid": {"ys": ys, "xs": xs}, "header_rows": 1,
             "columns": [
                 {"idx": 0, "name": "material", "kind": "printed"},
                 {"idx": 1, "name": "level", "kind": "printed"},
                 {"idx": 2, "name": "trips_day", "kind": "handwritten_number", "shift": "day"},
                 {"idx": 3, "name": "trips_night", "kind": "handwritten_number", "shift": "night"},
             ],
             "rows": _haul_rows(rows)},
            {"name": "side", "grid": {"ys": sys_, "xs": sxs}, "header_rows": 0,
             "columns": [{"idx": 0, "name": "label", "kind": "printed"},
                         {"idx": 1, "name": "value", "kind": "handwritten_text"}],
             "rows": [{"row": i, "key": s, "label": s} for i, s in enumerate(SIDE_LABELS)]},
        ],
        "fields": [
            {"name": "date_line", "kind": "handwritten_text", "bbox": [240, 210, 720, 275]},
            {"name": "vehicle_no", "kind": "handwritten_text", "bbox": [340, 290, 730, 355], "meta_key": "vehicle_no"},
            {"name": "operator", "kind": "handwritten_text", "bbox": [920, 290, 1290, 355], "meta_key": "operator"},
        ],
    }
    if meta:
        spec["fields"] = [
            {"name": "date_month", "kind": "handwritten_number", "bbox": list(META_BOXES["date_month"]), "meta_key": "date.month"},
            {"name": "date_day", "kind": "handwritten_number", "bbox": list(META_BOXES["date_day"]), "meta_key": "date.day"},
            {"name": "vehicle_no", "kind": "handwritten_text", "bbox": list(META_BOXES["vehicle_no"]), "meta_key": "vehicle_no"},
            {"name": "operator", "kind": "handwritten_text", "bbox": list(META_BOXES["operator"]), "meta_key": "operator"},
        ]
    return img, spec


def build_haul_matrix(slots: list[tuple[str, str, str]] = SLOTS, name: str = T_MATRIX,
                      valid: tuple[str | None, str | None] | None = None, low: bool = False) -> tuple[np.ndarray, dict]:
    """slots: 인쇄된 머리글 (자리, 차량, 운전자). valid=(valid_from, valid_to) 를 주면 개정판 계열의 한 판이 된다.
    low: 낮은 칸 (행 높이 LOW_ROW_H, 숫자 칸 폭 LOW_NUM_W)."""
    img = _canvas(LANDSCAPE)
    _label(img, "LOADER DAILY LOG  -  UNDERGROUND LOADING", 150, 170, 1.4, 3)
    _label(img, "Date:  20      .        .", 150, 265, 0.9)
    _label(img, "Loader operator:", 760, 265, 0.9)
    if low:
        xs = [150, 400, 600] + [600 + LOW_NUM_W * (k + 1) for k in range(len(SLOTS))]
        ys = [330, 370, 410] + [410 + LOW_ROW_H * (i + 1) for i in range(len(UG_ROWS))]
    else:
        xs = [150, 400, 600] + [600 + 200 * (k + 1) for k in range(len(SLOTS))]
        ys = [330, 390, 450] + [450 + 80 * (i + 1) for i in range(len(UG_ROWS))]
    hs = 0.45 if low else 0.65
    _grid(img, ys, xs)
    _cell_label(img, "Material", ys, xs, 0, 0)
    _cell_label(img, "Level", ys, xs, 0, 1)
    columns = [{"idx": 0, "name": "material", "kind": "printed"}, {"idx": 1, "name": "level", "kind": "printed"}]
    for k, (slot, vehicle, operator) in enumerate(slots):
        _cell_label(img, f"{slot} {vehicle}" if low else f"{slot}  {vehicle}", ys, xs, 0, 2 + k, hs)
        _cell_label(img, operator, ys, xs, 1, 2 + k, hs)
        columns.append({"idx": 2 + k, "name": f"slot_{slot}", "kind": "handwritten_number", "slot": slot,
                        "header_vehicle_no": vehicle, "header_operator": operator})
    for i, (m, lv) in enumerate(UG_ROWS):
        _cell_label(img, m, ys, xs, i + 2, 0, 0.55 if low else 0.7)
        _cell_label(img, lv, ys, xs, i + 2, 1, 0.55 if low else 0.7)
    rx = xs[-1] + 120
    cv2.rectangle(img, (rx, 330), (2180, 900), 0, 2)
    _label(img, "Remarks", rx + 20, 375, 0.9)
    _label(img, "Trips loaded onto each truck, by level.", rx, 960, 0.7)
    _label(img, "Totals must agree with the truck logs.", rx, 1000, 0.7)
    _label(img, "Form SYN-LOAD-02 rev.1", rx, 1040, 0.7)

    spec = {
        "name": name, "title": "Loader daily log, underground (synthetic)", "reference_image": "reference.png",
        "dpi": DPI, "page_size": list(LANDSCAPE), "handler": "haul",
        "handler_options": {"role": "matrix", "region": "matrix"},
        "regions": [{"name": "matrix", "grid": {"ys": ys, "xs": xs}, "header_rows": 2,
                     "columns": columns, "rows": _haul_rows(UG_ROWS)}],
        "fields": [
            {"name": "date_line", "kind": "handwritten_text", "bbox": [240, 220, 720, 285]},
            {"name": "operator", "kind": "handwritten_text", "bbox": [1010, 220, 1400, 285]},
        ],
    }
    if valid is not None:
        spec["family"] = MATRIX_FAMILY
        spec["valid_from"], spec["valid_to"] = valid
    return img, spec


BUILDERS = {T_INSP: build_inspection, T_LOG: build_haul_log, T_MATRIX: build_haul_matrix}

SITE_TOML = r"""# 합성 사이트 팩 — `minedocscan synth` 가 만든 시험용 데이터. 실제 현장이 아니다.
[site]
name = "synthetic"
title = "Synthetic mine (generated test data)"

[ingest]
# 하루치 묶음 파일명: scan_YYYY-MM-DD.pdf
date_from_filename = '(?P<yyyy>\d{4})-(?P<mm>\d{2})-(?P<dd>\d{2})'

[equipment.iso_type]
# 현장의 장비 구분 → ISO 23725 장비 유형. 대응을 확인하지 못한 구분은 적지 않는다
"Drill" = "Drill"
"Loader" = "Loader"
"Excavator" = "Excavator"
"LightVehicle" = "LightVehicle"

[haul]
# 운반 횟수의 범위: 이보다 큰 값은 인식기의 신뢰도가 높아도 검수로 보낸다 (숫자 인식기 — tasks/0003 4.4)
trips_max = 40

[crosscheck.haul]
# SURFACE 행은 일보에만 있어 행렬과 비교할 수 없다
exclude_materials = ["SURFACE"]

[eval]
# 평가셋 분할: 날짜 단위, hash(split_salt, 날짜) 로 정한다. 소금값을 바꾸면 평가셋이 바뀐다 (ADR 0009)
split_salt = "synthetic-2030"
test_share = 0.2
"""


def _blank_forms(low: bool = False, meta: bool = False) -> dict:
    if meta:                           # 메타 필드 합성에는 점검표가 없다 (그 시험에 쓰지 않는다 — 쪽마다 시간만 든다)
        return {T_LOG: build_haul_log(low, meta=True), T_MATRIX: build_haul_matrix(SLOTS_META, low=low)}
    return {T_INSP: build_inspection(), T_LOG: build_haul_log(low), T_MATRIX: build_haul_matrix(low=low)}


def write_site_pack(site_dir: str | Path, revision_from: str | None = None, low: bool = False, meta: bool = False,
                    usage: bool = False) -> Path:
    """합성 사이트 팩(site.toml + 템플릿 세 종)을 쓴다. revision_from(날짜)을 주면 행렬 양식이 두 판이 된다:
    그 전날까지 v1, 그날부터 v2 (같은 계열, 유효 기간으로 가린다). low: 운반 양식 두 종이 낮은 칸.
    meta: 일보에 월·일 필드, 차량번호가 네 자리 숫자인 행렬 머리글 (tasks/0004 단계 2).
    usage: 가동 일보 두 종(tools/synth_usage.py)과 장비명 대응표 [equipment.aliases] (tasks/0005)."""
    site = Path(site_dir)
    site.mkdir(parents=True, exist_ok=True)
    toml = SITE_TOML
    if usage:
        from . import synth_usage

        toml += synth_usage.USAGE_TOML
    (site / "site.toml").write_text(toml, encoding="utf-8")
    built = _blank_forms(low, meta)
    if usage:
        built.update({name: b() for name, b in synth_usage.BUILDERS.items()})
    if revision_from:
        last_v1 = (date.fromisoformat(revision_from) - timedelta(days=1)).isoformat()
        built[T_MATRIX] = build_haul_matrix(SLOTS, T_MATRIX, (None, last_v1), low=low)
        built[T_MATRIX_V2] = build_haul_matrix(SLOTS_V2, T_MATRIX_V2, (revision_from, None), low=low)
    for name, (img, spec) in built.items():
        d = site / "templates" / name
        d.mkdir(parents=True, exist_ok=True)
        imwrite(d / "reference.png", img)
        (d / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (site / "labels").mkdir(exist_ok=True)
    (site / "expected").mkdir(exist_ok=True)
    return site


# ── 하루치 내용(정답) 만들기 ───────────────────────────────────────────────
def _day_truth(d: int, day: str, rng, slots: list[tuple[str, str, str]] = SLOTS, meta: bool = False) -> dict:
    """d 번째 날의 정답. 날마다 다른 어려움을 넣는다 (d % 3). slots 는 그날 유효한 행렬 판의 머리글.
    meta: 메타 필드 합성의 배차 (d % 4): 1 = 두 사람이 차를 바꿔 탄다, 2 = 처음 보는 차, 3 = 처음 보는 사람이 쓴다."""
    scen = []
    # 점검표
    unused = d % 3 == 1
    if unused:
        scen.append("inspection_column_unused")
    insp = []
    for cat, _model, reg in EQUIPMENT:
        if not cat:
            continue
        abnormal = None if unused else bool(rng.random() < 0.2)
        if abnormal:
            remark = str(rng.choice(FAULT_PHRASES))
        else:
            remark = str(rng.choice(OK_PHRASES)) if rng.random() < 0.6 else ""
        insp.append({"date": day, "row_key": reg, "abnormal": abnormal, "remark": remark})

    # 그날의 실제 배차 (인쇄된 머리글과 다를 수 있다)
    trucks = []
    for slot, vehicle, operator in slots:
        t = {"slot": slot, "vehicle_no": vehicle, "operator": operator, "matched_by": "operator", "has_log": True}
        if meta:
            _meta_scenario(d, t, slots, scen)
            trucks.append(t)
            continue
        if d % 3 == 1 and slot == "T03":
            t["vehicle_no"] = "V-909"                      # 같은 운전자가 다른 차를 몬다
            scen.append("T03_vehicle_changed")
        if d % 3 == 2 and slot == "T04":
            t.update(operator="ECHO", matched_by="vehicle")  # 같은 차를 다른 운전자가 몬다
            scen.append("T04_operator_changed")
        if d % 3 == 2 and slot == "T02":
            t["has_log"] = False                            # 일보를 내지 않았다
            scen.append("T02_log_missing")
        trucks.append(t)

    scen = list(dict.fromkeys(scen))
    # 운반 횟수: 일보(차량·광종·편·근무조) → 행렬(자리·광종·편) 은 그 합
    value_rows = [r for r in UG_ROWS if r != NOTE_ROW]
    log, matrix = [], {}
    for t in trucks:
        picks = rng.choice(len(value_rows), size=int(rng.integers(4, 7)), replace=False)
        for ri in sorted(int(i) for i in picks):
            m, lv = value_rows[ri]
            day_trips = int(rng.integers(10, 15)) if rng.random() < 0.1 else int(rng.integers(1, 10))
            entries = [("day", day_trips)]
            if rng.random() < 0.3:
                entries.append(("night", int(rng.integers(1, 6))))
            matrix[(t["slot"], m, lv)] = sum(v for _s, v in entries)
            if t["has_log"]:
                log += [{"slot": t["slot"], "material": m, "level": lv, "shift": s, "trips": v} for s, v in entries]
        if t["has_log"] and rng.random() < 0.5:
            log.append({"slot": t["slot"], "material": SURFACE_ROW[0], "level": SURFACE_ROW[1], "shift": "day",
                        "trips": int(rng.integers(1, 6))})

    # 두 양식이 서로 다른 칸: 행렬에 빠뜨린 칸 2개(값 유무로 드러난다) + 횟수가 다른 칸 1개(숫자를 읽어야 드러난다)
    with_log = {t["slot"] for t in trucks if t["has_log"]}
    cands = sorted(k for k in matrix if k[0] in with_log)
    order = [cands[int(i)] for i in rng.permutation(len(cands))[:3]]
    disc = []
    for k in order[:2]:
        del matrix[k]
        disc.append({"slot": k[0], "material": k[1], "level": k[2], "type": "missing_in_matrix"})
    for k in order[2:3]:
        matrix[k] += 1
        disc.append({"slot": k[0], "material": k[1], "level": k[2], "type": "different_count"})

    return {"date": day, "scenarios": scen, "inspection": insp, "trucks": trucks, "haul_log": log,
            "haul_matrix": [{"slot": k[0], "material": k[1], "level": k[2], "trips": v} for k, v in sorted(matrix.items())],
            "discrepancies": disc,
            "headers": {slot: [vehicle, operator] for slot, vehicle, operator in slots}}


def _meta_scenario(d: int, t: dict, slots, scen: list) -> None:
    """메타 필드 합성의 배차 변화 (그날의 실제 차·사람). 행렬 머리글은 그대로다."""
    header = {s: (v, o) for s, v, o in slots}
    if d % 4 == 1 and t["slot"] in ("T03", "T04"):          # 두 사람이 차를 바꿔 탄다 (같은 사람, 다른 차)
        other = "T04" if t["slot"] == "T03" else "T03"
        t["vehicle_no"] = header[other][0]
        scen.append("T03_T04_swapped_vehicles")
    if d % 4 == 2 and t["slot"] == "T02":                   # 처음 보는 차 (목록에 없는 값)
        t["vehicle_no"] = synth_meta.NEW_VEHICLES[0]
        scen.append("T02_new_vehicle")
    if d % 4 == 3 and t["slot"] == "T04":                   # 처음 보는 사람이 같은 차를 몬다 → 차량번호로 자리를 찾는다
        t.update(operator=synth_meta.STRANGERS[0], matched_by="vehicle")
        scen.append("T04_new_operator")


def expected_xcheck(days: list[dict], with_trips: bool) -> dict[str, int]:
    """정답에서 교차검증 결과를 독립적으로 계산한다 (validate/crosscheck.py 와 다른 코드로).

    with_trips=False: 값 유무만 비교 (인식기가 없을 때). True: 횟수까지 비교 (오라클 백엔드).
    """
    counts: dict[str, int] = {}
    for dt in days:
        matrix = {(r["slot"], r["material"], r["level"]): r["trips"] for r in dt["haul_matrix"]}
        log: dict[tuple, int] = {}
        for r in dt["haul_log"]:
            k = (r["slot"], r["material"], r["level"])
            log[k] = log.get(k, 0) + r["trips"]
        for t in dt["trucks"]:
            for m, lv in UG_ROWS:
                k = (t["slot"], m, lv)
                if not t["has_log"]:
                    status = "missing_log"
                else:
                    lt, mt = log.get(k), matrix.get(k)
                    same = (lt is None) == (mt is None) and (not with_trips or lt == mt)
                    status = "match" if same else "mismatch"
                counts[status] = counts.get(status, 0) + 1
    return counts


# ── 채워진 페이지 그리기 ───────────────────────────────────────────────────
def _fill_inspection(blank, spec, dt, rng) -> np.ndarray:
    img = blank.copy()
    reg = spec["regions"][0]
    ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
    y, m, dd = dt["date"].split("-")
    _hand(img, f"{y[2:]}   {m}   {dd}", 250, 345, rng, 1.1)
    by_key = {r["row_key"]: r for r in dt["inspection"]}
    for row in reg["rows"]:
        t = by_key.get(row["key"])
        if t is None:
            continue
        gi = row["row"] + 1
        if t["remark"]:
            _hand_in_cell(img, t["remark"], _cell_bbox(ys, xs, gi, 3), rng, 1.2)
        if t["abnormal"] is True:
            _tick(img, _cell_bbox(ys, xs, gi, 4), rng, cross_right=bool(rng.random() < 0.7))
        elif t["abnormal"] is False:
            _tick(img, _cell_bbox(ys, xs, gi, 5), rng)
    return img


def _rough_number(img: np.ndarray, text: str, bbox, rng) -> None:
    """낮은 칸의 거친 숫자: 칸 높이의 1.0–1.25배로 써서 위아래 괘선을 넘는다. 가로로는 칸 안에 머문다
    (imaging/blobs.py 가 가로로 칸 폭의 절반까지 붙여 묶기 때문 — 이웃 칸 숫자와 한 덩어리가 되면 메모로 판정된다).
    세로로 더 넘치면 윗칸·아랫칸의 숫자와 붙어 한 덩어리가 되고, 그러면 이 칸은 값 없음으로 판정된다 (1.35배·±3 px 에서
    받침 있는 1 이 윗칸 9 에 붙어 사라진 일이 있었다) — oracle 로 CER 0 을 확인하는 양식이라 그 경우는 만들지 않는다."""
    x0, y0, x1, y1 = bbox
    ch = y1 - y0
    h = ch * float(rng.uniform(1.0, 1.25))
    cx, cy = (x0 + x1) / 2 + rng.uniform(-5, 5), (y0 + y1) / 2 + rng.uniform(-2, 2)
    m = int(h * 1.5)
    ox, oy = int(cx) - m, int(cy) - m
    ink = np.zeros((2 * m, 2 * m), np.float32)
    style = synth_cells._style(rng)
    style.update(gap=float(rng.uniform(0.04, 0.15)), ink=float(rng.uniform(0.75, 1.0)))
    synth_cells._draw_number(ink, text, cx - ox, cy - oy, h, rng, style)
    _composite(img, ink, ox, oy, int(rng.integers(20, 70)))


def _x_mark(img: np.ndarray, bbox, rng) -> None:
    """빈 칸의 X 표: 잉크로는 값이 있어 보이지만 이 칸의 값이 아니다."""
    x0, y0, x1, y1 = bbox
    ch = y1 - y0
    s = ch * float(rng.uniform(0.8, 1.2))
    a = s * float(rng.uniform(0.8, 1.4))
    cx, cy = (x0 + x1) / 2 + rng.uniform(-6, 6), (y0 + y1) / 2 + rng.uniform(-2, 2)
    c = int(rng.integers(20, 70))
    th = int(rng.integers(2, 4))
    cv2.line(img, (int(cx - a / 2), int(cy - s / 2)), (int(cx + a / 2), int(cy + s / 2)), c, th, cv2.LINE_AA)
    cv2.line(img, (int(cx + a / 2), int(cy - s / 2)), (int(cx - a / 2), int(cy + s / 2)), c, th, cv2.LINE_AA)


def _composite(img: np.ndarray, ink: np.ndarray, ox: int, oy: int, dark: int) -> None:
    H, W = img.shape
    hh, ww = ink.shape
    sx0, sy0, sx1, sy1 = max(0, ox), max(0, oy), min(W, ox + ww), min(H, oy + hh)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    patch = ink[sy0 - oy:sy1 - oy, sx0 - ox:sx1 - ox]
    val = (255 - np.clip(patch, 0, 1) * (255 - dark)).astype(np.uint8)
    img[sy0:sy1, sx0:sx1] = np.minimum(img[sy0:sy1, sx0:sx1], val)


def _x_marks(img: np.ndarray, empty_cells: list, rng, n_max: int = 2) -> list:
    """빈 숫자 칸 중 몇 개(최대 n_max)에 X 표를 그린다. 그린 칸을 돌려준다."""
    if not empty_cells:
        return []
    k = int(rng.integers(0, n_max + 1))
    picks = [empty_cells[int(i)] for i in rng.choice(len(empty_cells), size=min(k, len(empty_cells)), replace=False)]
    for bbox in picks:
        _x_mark(img, bbox, rng)
    return picks


def _fill_log(blank, spec, dt, truck, rng, low: bool = False, meta: bool = False) -> np.ndarray:
    img = blank.copy()
    haul, side = spec["regions"]
    ys, xs = haul["grid"]["ys"], haul["grid"]["xs"]
    y, m, dd = dt["date"].split("-")
    if meta:                                     # 그날 그 차를 몬 사람이 자기 글씨로 쓴다
        style = synth_meta.page_style(synth_meta.writer_style(truck["operator"]), rng)
        _hand(img, y[2:], 262, 262, rng, 1.0)
        for name, text, kind in (("date_month", str(int(m)), "digits"), ("date_day", str(int(dd)), "digits"),
                                 ("vehicle_no", truck["vehicle_no"], "digits"), ("operator", truck["operator"], "name")):
            _meta_field(img, META_BOXES[name], text, kind, style, rng)
    else:
        _hand(img, f"{y[2:]}   {m}   {dd}", 300, 262, rng, 1.1)
        _hand(img, truck["vehicle_no"], 370, 340, rng, 1.2)
        _hand(img, truck["operator"].title(), 950, 340, rng, 1.2)
    row_of = {r["key"]: r["row"] for r in haul["rows"]}
    filled = set()
    for r in dt["haul_log"]:
        if r["slot"] != truck["slot"]:
            continue
        gi = row_of[f"{r['material']}|{r['level']}"] + 1
        ci = 2 if r["shift"] == "day" else 3
        filled.add((gi, ci))
        if low:
            _rough_number(img, str(r["trips"]), _cell_bbox(ys, xs, gi, ci), rng)
        else:
            _hand_in_cell(img, str(r["trips"]), _cell_bbox(ys, xs, gi, ci), rng, 1.5, 3, center=True)
    if low:
        _x_marks(img, [_cell_bbox(ys, xs, gi, ci) for gi in range(1, len(haul["rows"]) + 1) for ci in (2, 3)
                       if (gi, ci) not in filled], rng)
    sys_, sxs = side["grid"]["ys"], side["grid"]["xs"]
    for i, s in enumerate((str(int(rng.integers(40, 160))), f"{rng.uniform(4, 11):.1f}")):
        _hand_in_cell(img, s, _cell_bbox(sys_, sxs, i, 1), rng, 1.3)
    return img


def _meta_field(img: np.ndarray, box, text: str, kind: str, style: dict, rng) -> None:
    """메타 필드 하나를 쪽에 쓴다 (synth_meta 의 획 — 학습용 크롭과 같은 그리기)."""
    x0, y0, x1, y1 = box
    margin = 12
    ink = synth_meta.render_field_ink(text, kind, x1 - x0, y1 - y0, margin, 2.0, style, rng)
    ink = cv2.resize(ink, (x1 - x0 + 2 * margin, y1 - y0 + 2 * margin), interpolation=cv2.INTER_AREA)
    _composite(img, ink, x0 - margin, y0 - margin, int(rng.integers(20, 60)))


def _fill_matrix(blank, spec, dt, rng, low: bool = False) -> np.ndarray:
    img = blank.copy()
    reg = spec["regions"][0]
    ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
    y, m, dd = dt["date"].split("-")
    _hand(img, f"{y[2:]}   {m}   {dd}", 300, 272, rng, 1.1)
    _hand(img, "Foxtrot", 1040, 270, rng, 1.2)
    row_of = {r["key"]: r["row"] for r in reg["rows"]}
    col_of = {c["slot"]: c["idx"] for c in reg["columns"] if "slot" in c}
    filled = set()
    for r in dt["haul_matrix"]:
        gi = row_of[f"{r['material']}|{r['level']}"] + 2
        filled.add((gi, col_of[r["slot"]]))
        if low:
            _rough_number(img, str(r["trips"]), _cell_bbox(ys, xs, gi, col_of[r["slot"]]), rng)
        else:
            _hand_in_cell(img, str(r["trips"]), _cell_bbox(ys, xs, gi, col_of[r["slot"]]), rng, 1.5, 3, center=True)
    # 여러 칸에 걸친 메모 — 셀 값으로 세면 안 된다
    gi = row_of[f"{NOTE_ROW[0]}|{NOTE_ROW[1]}"] + 2
    if low:
        _hand(img, "closed for blasting", xs[2] + 30, ys[gi + 1] - 6, rng, 1.0, 2)
        note_row = gi
        _x_marks(img, [_cell_bbox(ys, xs, g, c) for g in range(2, len(reg["rows"]) + 2) for c in col_of.values()
                       if (g, c) not in filled and g != note_row], rng)
    else:
        _hand(img, "closed for blasting", xs[2] + 30, ys[gi + 1] - 22, rng, 1.5, 3)
    return img


def _write_pdf(path: Path, pages: list[np.ndarray], dpi: int = DPI) -> None:
    import pymupdf

    doc = pymupdf.open()
    for img in pages:
        h, w = img.shape
        page = doc.new_page(width=w * 72 / dpi, height=h * 72 / dpi)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])    # 복합기 스캔처럼 JPEG 로 담는다
        if not ok:
            raise ValueError("페이지를 인코딩할 수 없습니다")
        page.insert_image(page.rect, stream=buf.tobytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path), no_new_id=True)      # 저장할 때 새 /ID 를 넣지 않는다: 같은 seed 면 바이트까지 같아야 문서 ID 가 같다
    doc.close()


def _meta_answers(source: str, day: str, t: dict) -> list[dict]:
    """메타 필드의 정답 (eval·oracle 형식): 종이에 적힌 값 — 월·일은 쪽의 날짜에서."""
    _y, m, dd = day.split("-")
    vals = {"date_month": str(int(m)), "date_day": str(int(dd)), "vehicle_no": t["vehicle_no"], "operator": t["operator"]}
    return [{"source": source, "template": T_LOG, "region": "fields", "field_name": k, "row_key": "", "text": v}
            for k, v in vals.items()]


def _add_usage_pages(add, plans: list, blanks: dict, rng, stem: str, day: str, answers: list, truth: list) -> None:
    """그날의 가동 일보 쪽을 묶음에 붙인다 (그리기·스캔 효과 모두 가동 일보의 난수로). 정답과 쪽 정답을 모은다."""
    from . import synth_usage

    for p in plans:
        img = synth_usage.fill_page(*blanks[p.template], p, rng)
        n = add(img, p.template, _rng=rng, equipment=p.equipment)
        source = f"{stem}#{n}"
        answers += synth_usage.answers_of(source, p)
        truth.append(synth_usage.truth_of(source, day, p))


@dataclass
class SynthResult:
    root: Path
    site: Path               # 사이트 팩
    scans: Path              # 스캔 문서(PDF) 폴더 — ARCHIVE_ROOT 로 쓴다
    truth_path: Path
    answers_path: Path       # 오라클 백엔드용 정답 (recognize.builtin.load_answers_json)
    truth: dict


def generate(out_dir: str | Path, days: int = 3, seed: int = 0, start: str = "2030-01-07",
             strength: float = 1.0, matrix_revision: bool = False, low_cells: bool = False,
             meta_fields: bool = False, mix_pages: bool = False, usage_logs: bool = False,
             usage_only: bool = False) -> SynthResult:
    """out_dir 에 합성 사이트 팩(site/)과 스캔 문서(scans/), 정답(truth.json, answers.json)을 만든다.

    하루에 PDF 한 개: 점검표 1장 → 차량별 일보(일보를 낸 차량 수) → 행렬 1장.
    같은 seed 는 같은 결과를 낸다. matrix_revision=True 면 둘째 날부터 행렬 양식이 개정판(v2)이다 — 기본 데이터는 그대로다.
    low_cells=True 면 운반 양식 두 종이 낮은 칸·거친 숫자·X 표 (tasks/0003 단계 3). X 표 칸은 정답에 없다(빈 칸)
    — 잉크로는 값이 있어 보이므로 그 날의 truth["expected"] 의 교차검증 수치와는 맞지 않는다.
    meta_fields=True 면 일보의 메타 필드가 사람마다 다른 획 (tasks/0004 단계 2), mix_pages=True 면 마지막 날의 묶음에
    첫날의 일보 한 쪽이 섞인다 — truth["documents"] 에 mixed_from 으로 표시한다. 정답(answers.json)에 메타 필드의 값이 들어간다.
    usage_logs=True 면 날마다 묶음 끝에 가동 일보(tools/synth_usage.py)를 붙인다 — 난수를 따로 쓰므로 앞의 쪽은 그대로다.
    usage_only=True 면 가동 일보만 (점검표·운반 쪽 없이 — 시험 시간을 아낀다. 사이트 팩의 템플릿은 그대로 다 쓴다).
    가동 일보의 정답: truth["usage"] (쪽마다 eq_usage_daily 의 정답), answers.json 에 그 칸들.
    """
    if mix_pages and not meta_fields:
        raise ValueError("mix_pages 는 meta_fields 와 같이 쓴다")
    usage_logs = usage_logs or usage_only
    root = Path(out_dir)
    d0 = date.fromisoformat(start)
    revision_from = (d0 + timedelta(days=1)).isoformat() if matrix_revision else None
    site = write_site_pack(root / "site", revision_from=revision_from, low=low_cells, meta=meta_fields, usage=usage_logs)
    scans = root / "scans"
    rng = np.random.default_rng(seed)
    blanks = _blank_forms(low_cells, meta_fields)
    if revision_from:
        blanks[T_MATRIX_V2] = build_haul_matrix(SLOTS_V2, T_MATRIX_V2, (revision_from, None), low=low_cells)

    day_truths, labels, answers, documents = [], {}, [], {}
    insp_blank = build_inspection() if meta_fields else None
    usage_plan, usage_truth = None, []
    if usage_logs:
        from . import synth_usage

        rng_u = np.random.default_rng([seed, 5005])               # 가동 일보는 따로 — 앞의 쪽의 난수 흐름을 건드리지 않는다
        usage_blanks = {name: b() for name, b in synth_usage.BUILDERS.items()}
        usage_plan = synth_usage.plan_days([(d0 + timedelta(days=d)).isoformat() for d in range(days)], rng_u)
    for d in range(days):
        day = (d0 + timedelta(days=d)).isoformat()
        v2 = bool(revision_from) and day >= revision_from
        matrix_name, slots = (T_MATRIX_V2, SLOTS_V2) if v2 else (T_MATRIX, SLOTS)
        if meta_fields:
            slots = SLOTS_META
        stem = f"scan_{day}"
        pages, page_info = [], []

        def add(img, template, _pages=pages, _info=page_info, _rng=rng, **extra):
            _pages.append(scan_effect(img, _rng, strength))
            _info.append({"page": len(_pages), "template": template, **extra})
            return len(_pages)

        if usage_only:
            _add_usage_pages(add, usage_plan[d], usage_blanks, rng_u, stem, day, answers, usage_truth)
            _write_pdf(scans / f"{stem}.pdf", pages)
            documents[stem] = page_info
            continue
        dt = _day_truth(d, day, rng, slots, meta=meta_fields)

        if meta_fields:                # 점검표 쪽은 넣지 않지만 난수는 같은 만큼 쓴다 (나머지 쪽의 글씨가 그대로이게)
            scan_effect(_fill_inspection(*insp_blank, dt, rng), rng, strength)
        else:
            add(_fill_inspection(*blanks[T_INSP], dt, rng), T_INSP)
            for r in dt["inspection"]:
                answers.append({"work_date": day, "template": T_INSP, "region": "main", "field_name": "remark",
                                "row_key": r["row_key"], "text": r["remark"]})
        for t in dt["trucks"]:
            if not t["has_log"]:
                continue
            n = add(_fill_log(*blanks[T_LOG], dt, t, rng, low=low_cells, meta=meta_fields), T_LOG, slot=t["slot"])
            source = f"{stem}#{n}"
            labels[source] = {"vehicle_no": t["vehicle_no"], "operator": t["operator"]}
            if meta_fields:
                answers += _meta_answers(source, dt["date"], t)
            for r in dt["haul_log"]:
                if r["slot"] == t["slot"]:
                    r["source"] = source
                    answers.append({"source": source, "template": T_LOG, "region": "haul",
                                    "field_name": f"trips_{r['shift']}", "row_key": f"{r['material']}|{r['level']}",
                                    "text": str(r["trips"])})
        if mix_pages and d == days - 1 and day_truths:        # 첫날의 일보 한 쪽이 섞였다 (적힌 날짜가 다르다)
            first = day_truths[0]
            t0 = next(t for t in first["trucks"] if t["has_log"])
            n = add(_fill_log(*blanks[T_LOG], first, t0, rng, low=low_cells, meta=True), T_LOG, slot=t0["slot"],
                    mixed_from=first["date"])
            labels[f"{stem}#{n}"] = {"vehicle_no": t0["vehicle_no"], "operator": t0["operator"]}
        n = add(_fill_matrix(*blanks[matrix_name], dt, rng, low=low_cells), matrix_name)
        for r in dt["haul_matrix"]:
            answers.append({"source": f"{stem}#{n}", "template": matrix_name, "region": "matrix",
                            "field_name": f"slot_{r['slot']}", "row_key": f"{r['material']}|{r['level']}",
                            "text": str(r["trips"])})
        if usage_logs:
            _add_usage_pages(add, usage_plan[d], usage_blanks, rng_u, stem, day, answers, usage_truth)
        _write_pdf(scans / f"{stem}.pdf", pages)
        documents[stem] = page_info
        day_truths.append(dt)

    (site / "labels" / "pages.json").write_text(json.dumps(labels, ensure_ascii=False, indent=1), encoding="utf-8")
    insp = [r for dt in day_truths for r in dt["inspection"]]
    pages_by_form: dict[str, int] = {}
    for info in documents.values():
        for p in info:
            pages_by_form[p["template"]] = pages_by_form.get(p["template"], 0) + 1
    truth = {
        "seed": seed, "start": start, "n_days": days, "low_cells": low_cells, "documents": documents, "days": day_truths,
        "expected": {
            "documents": days,
            "pages": sum(pages_by_form.values()),
            "pages_by_form": pages_by_form,
            "inspection": {"rows": len(insp),
                           "abnormal_yes": sum(r["abnormal"] is True for r in insp),
                           "abnormal_no": sum(r["abnormal"] is False for r in insp),
                           "abnormal_undecided": sum(r["abnormal"] is None for r in insp)},
            "xcheck_haul_has_only": expected_xcheck(day_truths, with_trips=False),
            "xcheck_haul_with_trips": expected_xcheck(day_truths, with_trips=True),
            "assignments": {"n": sum(t["has_log"] for dt in day_truths for t in dt["trucks"]),
                            "header_mismatch": sum(
                                t["has_log"] and [t["vehicle_no"], t["operator"]] != dt["headers"][t["slot"]]
                                for dt in day_truths for t in dt["trucks"])},
        },
    }
    if meta_fields:                                   # 기본 데이터의 truth.json 은 바이트까지 그대로
        truth["meta_fields"] = True
    if usage_logs:
        truth["usage"] = usage_truth
    truth_path, answers_path = root / "truth.json", root / "answers.json"
    truth_path.write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    answers_path.write_text(json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8")
    return SynthResult(root, site, scans, truth_path, answers_path, truth)
