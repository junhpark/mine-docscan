"""가상 양식 두 종 (tasks/0009 4.7 가): 유류일지(fuel_log)·환경일지(env_log) — 확장성의 증거 (ROADMAP V2).

새 양식이 **템플릿만으로** 들어가 적재·엑셀·통합 DB 까지 나간다는 것을 보인다 — 파이프라인 코드는 이 이름들을 모른다 (원칙 1).
그래서 양식 이름과 필드 이름은 이 둘에만 있는 접두(fuel_·env_)를 쓴다 — 시험이 src/ 에서 tools/ 밖에 그 이름이 없는지 본다.
핸들러는 generic (업무 테이블 없음 — doc_field·엑셀의 양식 시트·통합 DB 의 doc_field 까지). 날마다 한 장씩.
기계가 읽는 칸(형식 없음·integer)이 대부분이고 읽지 않는 형식은 몇 칸만 둔다 — 주유 시각(time), pH(decimal): "잉크 있음 + 검수 대기"의 길.
인쇄된 값(장비명·지점명)은 템플릿의 행에서 확정한다 (원칙 2). 이름·번호는 합성 값뿐이다.
난수는 따로 쓴다 (synth.generate 가 [seed, 4004] 로) — 이 선택 없이 만든 묶음은 바이트까지 그대로.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from . import synth_meta
from .synth_usage import (
    DPI,
    PORTRAIT,
    _canvas,
    _cell_box,
    _grid,
    _label,
    _signature,
    _words,
    _write_in_cell,
)

T_FUEL, T_ENV = "fuel_log", "env_log"
FUEL_UNITS = ("EX-01", "LD-02", "DT-03", "DT-04", "GR-05", "WT-06")      # 인쇄된 장비 (합성)
FUEL_TYPES = ("diesel", "diesel", "diesel", "petrol")                        # 손으로 쓰는 유종 (handfont 의 소문자로 쓸 수 있는 낱말)
FUEL_BY = ("ALPHA", "BRAVO", "CHARLIE")                                      # 주유한 사람 (합성 이름)
ENV_POINTS = (("P-01", "Settling pond"), ("P-02", "Discharge"), ("P-03", "Upstream"), ("P-04", "Downstream"),
              ("P-05", "Portal"))
ENV_NOTES = ("clear", "brown", "foam", "low flow", "restart")
OPERATORS = ("ALPHA", "BRAVO", "CHARLIE", "DELTA")


def _date_line(img, y: int) -> tuple[list[int], list[int]]:
    """인쇄된 날짜 줄 "Date: 20__ . __ (month) . __ (day)" — 손으로 쓰는 월·일 칸의 bbox 둘."""
    _label(img, "Date:  2030 .", 100, y, 0.9)
    _label(img, "(month) .", 470, y, 0.8)
    _label(img, "(day)", 760, y, 0.8)
    month, day = [330, y - 50, 460, y + 14], [620, y - 50, 750, y + 14]
    for x0, _y0, x1, y1 in (month, day):
        cv2.line(img, (x0, y1 - 4), (x1, y1 - 4), 0, 2)
    return month, day


def build_fuel_log() -> tuple[np.ndarray, dict]:
    """유류일지: 장비 행 × (주유량 L — integer, 유종 — 글자, 주유자 — 글자, 주유 시각 — time(읽지 않는다), 서명). 표 밖: 날짜 줄, 작성자."""
    img = _canvas(PORTRAIT)
    _label(img, "FUEL ISSUE LOG", 100, 170, 1.4, 3)
    _label(img, "Site: SYNTHETIC MINE  (generated test data - not a real site)", 100, 225, 0.7)
    month, day = _date_line(img, 330)
    _label(img, "Recorded by:", 900, 330, 0.9)
    op = [1110, 280, 1540, 345]
    cv2.line(img, (op[0], op[3] - 4), (op[2], op[3] - 4), 0, 2)

    xs = [100, 330, 560, 820, 1110, 1330, 1554]              # Unit / Litres / Fuel / Issued by / Time / Sign
    ys = [420, 480] + [480 + 90 * (i + 1) for i in range(len(FUEL_UNITS))]
    _grid(img, ys, xs)
    for ci, s in enumerate(("Unit", "Litres", "Fuel", "Issued by", "Time", "Sign")):
        _label(img, s, xs[ci] + 14, ys[0] + 40, 0.7)
    rows = []
    for i, unit in enumerate(FUEL_UNITS):
        _label(img, unit, xs[0] + 14, ys[i + 1] + 55, 0.8)
        rows.append({"row": i, "key": unit, "fuel_unit": unit})
    bottom = ys[-1]
    _label(img, "Write litres as a whole number. Time as HH:MM.", 100, bottom + 60, 0.6)
    _label(img, "Form SYN-FUEL-01 rev.1", 100, bottom + 100, 0.6)
    spec = {
        "name": T_FUEL, "title": "Fuel issue log (synthetic V2 form)", "display": "유류일지", "reference_image": "reference.png",
        "dpi": DPI, "page_size": list(PORTRAIT), "handler": "generic", "handler_options": {},
        "regions": [
            {"name": "fuel_main", "display": "주유", "grid": {"ys": ys, "xs": xs}, "header_rows": 1,
             "columns": [{"idx": 0, "name": "fuel_unit", "kind": "printed", "display": "장비"},
                         {"idx": 1, "name": "fuel_litres", "kind": "handwritten_number", "format": "integer", "display": "주유량(L)"},
                         {"idx": 2, "name": "fuel_type", "kind": "handwritten_text", "display": "유종"},
                         {"idx": 3, "name": "fuel_by", "kind": "handwritten_text", "display": "주유자"},
                         {"idx": 4, "name": "fuel_time", "kind": "handwritten_number", "format": "time", "display": "주유 시각"},
                         {"idx": 5, "name": "fuel_sign", "kind": "signature", "display": "서명"}],
             "rows": rows},
        ],
        "fields": [
            {"name": "fuel_date_month", "kind": "handwritten_number", "bbox": month, "meta_key": "date.month", "display": "월"},
            {"name": "fuel_date_day", "kind": "handwritten_number", "bbox": day, "meta_key": "date.day", "display": "일"},
            {"name": "fuel_recorder", "kind": "handwritten_text", "bbox": op, "meta_key": "operator", "display": "작성자"},
        ],
    }
    return img, spec


def build_env_log() -> tuple[np.ndarray, dict]:
    """환경일지: 측정 지점 행 × (탁도 — integer, pH — decimal(읽지 않는다), 유량 — integer, 비고 — 글자). 두 줄 머리글. 확인자 서명."""
    img = _canvas(PORTRAIT)
    _label(img, "ENVIRONMENTAL MONITORING LOG", 100, 170, 1.4, 3)
    _label(img, "Site: SYNTHETIC MINE  (generated test data - not a real site)", 100, 225, 0.7)
    month, day = _date_line(img, 330)

    xs = [100, 520, 760, 960, 1200, 1554]                    # Point / Turbidity / pH / Flow / Remarks
    ys = [420, 470, 520] + [520 + 90 * (i + 1) for i in range(len(ENV_POINTS))]
    for y in ys:                                               # 두 줄 머리글: 첫 줄의 "Water quality" 는 탁도·pH 두 칸에 걸친다
        x_from = xs[1] if y == ys[1] else xs[0]
        x_to = xs[3] if y == ys[1] else xs[-1]
        cv2.line(img, (x_from, y), (x_to, y), 0, 2)
    for x in xs:
        y_from = ys[1] if x == xs[2] else ys[0]
        cv2.line(img, (x, y_from), (x, ys[-1]), 0, 2)
    _label(img, "Point", xs[0] + 14, ys[0] + 62, 0.7)
    _label(img, "Water quality", xs[1] + 60, ys[0] + 36, 0.7)
    _label(img, "Turbidity", xs[1] + 14, ys[1] + 36, 0.6)
    _label(img, "pH", xs[2] + 14, ys[1] + 36, 0.6)
    _label(img, "Flow m3/h", xs[3] + 14, ys[0] + 62, 0.6)
    _label(img, "Remarks", xs[4] + 14, ys[0] + 62, 0.7)
    rows = []
    for i, (code, name) in enumerate(ENV_POINTS):
        _label(img, code, xs[0] + 14, ys[i + 2] + 40, 0.75)
        _label(img, name, xs[0] + 14, ys[i + 2] + 75, 0.55)
        rows.append({"row": i, "key": code, "env_point": f"{code} {name}"})
    bottom = ys[-1]
    sig = (1150, bottom + 40, 1554, bottom + 120)
    _label(img, "Checked by (signature):", 820, bottom + 90, 0.7)
    cv2.rectangle(img, sig[:2], sig[2:], 0, 2)
    _label(img, "Turbidity in NTU (whole number), pH with one decimal.", 100, bottom + 170, 0.6)
    _label(img, "Form SYN-ENV-01 rev.1", 100, bottom + 210, 0.6)
    spec = {
        "name": T_ENV, "title": "Environmental monitoring log (synthetic V2 form)", "display": "환경일지",
        "reference_image": "reference.png",
        "dpi": DPI, "page_size": list(PORTRAIT), "handler": "generic", "handler_options": {},
        "regions": [
            {"name": "env_main", "display": "측정", "grid": {"ys": ys, "xs": xs}, "header_rows": 2,
             "columns": [{"idx": 0, "name": "env_point", "kind": "printed", "display": "지점"},
                         {"idx": 1, "name": "env_turbidity", "kind": "handwritten_number", "format": "integer", "display": "탁도(NTU)"},
                         {"idx": 2, "name": "env_ph", "kind": "handwritten_number", "format": "decimal", "display": "pH"},
                         {"idx": 3, "name": "env_flow", "kind": "handwritten_number", "format": "integer", "display": "유량(m3/h)"},
                         {"idx": 4, "name": "env_note", "kind": "handwritten_text", "display": "비고"}],
             "rows": rows},
        ],
        "fields": [
            {"name": "env_date_month", "kind": "handwritten_number", "bbox": month, "meta_key": "date.month", "display": "월"},
            {"name": "env_date_day", "kind": "handwritten_number", "bbox": day, "meta_key": "date.day", "display": "일"},
            {"name": "env_checker", "kind": "signature", "bbox": [sig[0] + 6, sig[1] + 6, sig[2] - 6, sig[3] - 6],
             "display": "확인자 서명"},
        ],
    }
    return img, spec


BUILDERS = {T_FUEL: build_fuel_log, T_ENV: build_env_log}


# ── 하루치 내용 ────────────────────────────────────────────────────────────
@dataclass
class V2Page:
    template: str
    day: str
    writer: str
    cells: dict = field(default_factory=dict)          # (행 키, 열 이름) → 적은 글자 (값이 있는 칸만)
    signed_rows: list = field(default_factory=list)    # 서명한 행 (유류일지)
    signed: bool = False                               # 확인자 서명 (환경일지)
    texts: dict = field(default_factory=dict)          # 표 밖 필드 → 글자


def plan_day(day: str, rng) -> list[V2Page]:
    """그날의 V2 쪽 둘 (유류일지·환경일지). 비는 칸이 섞인다 (그날 주유하지 않은 장비, 재지 않은 지점)."""
    _y, m, d = day.split("-")
    fuel = V2Page(T_FUEL, day, str(rng.choice(OPERATORS)))
    fuel.texts = {"fuel_date_month": str(int(m)), "fuel_date_day": str(int(d)), "fuel_recorder": fuel.writer}
    for unit in FUEL_UNITS:
        if rng.random() < 0.3:                          # 그날 주유하지 않았다 — 행 전체가 빈다
            continue
        hh, mm = int(rng.integers(6, 19)), int(rng.choice([0, 10, 15, 20, 30, 40, 45, 50]))
        fuel.cells[(unit, "fuel_litres")] = str(int(rng.integers(20, 400)))
        fuel.cells[(unit, "fuel_type")] = str(rng.choice(FUEL_TYPES))
        fuel.cells[(unit, "fuel_by")] = str(rng.choice(FUEL_BY))
        fuel.cells[(unit, "fuel_time")] = f"{hh:02d}:{mm:02d}"
        if rng.random() < 0.85:
            fuel.signed_rows.append(unit)
    env = V2Page(T_ENV, day, str(rng.choice(OPERATORS)))
    env.texts = {"env_date_month": str(int(m)), "env_date_day": str(int(d))}
    for code, _name in ENV_POINTS:
        if rng.random() < 0.2:                          # 재지 않은 지점
            continue
        env.cells[(code, "env_turbidity")] = str(int(rng.integers(1, 120)))
        env.cells[(code, "env_ph")] = f"{rng.uniform(6.0, 8.6):.1f}"
        env.cells[(code, "env_flow")] = str(int(rng.integers(5, 300)))
        if rng.random() < 0.35:
            env.cells[(code, "env_note")] = str(rng.choice(ENV_NOTES))
    env.signed = bool(rng.random() < 0.9)
    return [fuel, env]


def fill_page(blank: np.ndarray, spec: dict, p: V2Page, rng) -> np.ndarray:
    from .synth import _meta_field

    img = blank.copy()
    style = synth_meta.page_style(synth_meta.writer_style(p.writer), rng)
    fields = {f["name"]: f for f in spec["fields"]}
    reg = spec["regions"][0]
    rows = {r["key"]: r["row"] for r in reg["rows"]}
    for name, text in p.texts.items():
        box = tuple(fields[name]["bbox"])
        if text.isdigit():
            _write_in_cell(img, text, box, rng, style, (0.5, 0.65))
        else:
            _meta_field(img, box, text, "name", style, rng)
    for (rk, col), text in sorted(p.cells.items()):
        box = _cell_box(reg, rows[rk], col)
        if col in ("fuel_type", "fuel_by", "env_note"):
            x0, y0, _x1, y1 = box
            _words(img, text.lower(), x0 + 14, (y0 + y1) / 2, 34, rng, style)
        else:
            _write_in_cell(img, text, box, rng, style, (0.5, 0.62))
    for rk in p.signed_rows:
        _signature(img, _cell_box(reg, rows[rk], "fuel_sign", inset=10), rng)
    if p.signed and "env_checker" in fields:
        _signature(img, fields["env_checker"]["bbox"], rng)
    return img


def answers_of(source: str, p: V2Page) -> list[dict]:
    """eval·oracle 형식의 정답 (값이 있는 칸만, 사람이 입력할 표기로). 서명은 정답이 없다 (값 유무만)."""
    region = "fuel_main" if p.template == T_FUEL else "env_main"
    out = [{"source": source, "template": p.template, "region": region, "field_name": col, "row_key": rk, "text": text}
           for (rk, col), text in sorted(p.cells.items())]
    out += [{"source": source, "template": p.template, "region": "fields", "field_name": name, "row_key": "", "text": text}
            for name, text in p.texts.items()]
    return out


def truth_of(source: str, p: V2Page) -> dict:
    return {"source": source, "date": p.day, "template": p.template,
            "cells": {f"{rk}|{col}": text for (rk, col), text in sorted(p.cells.items())},
            "signed_rows": list(p.signed_rows), "signed": p.signed, "fields": dict(p.texts)}
