"""소수·시각을 기계가 읽기 (tasks/0011 단계 1) — 형식 칸의 길([recognize.by_format]), 칸의 규칙(handlers/base.format_row),
쪽 안 검산(handlers/usage.page_gate), 쓸 수 있는 표기(formats.spellings·same_value), 대기열·리포트·inked·info.

설정하지 않은 상태가 main 과 같다는 것은 같은 환경에서 main 과 이 브랜치로 만든 덤프를 견준다 (저장소에 지문을 두지 않는다 —
윈도우의 OpenCV 는 다르다). 여기서는 설정했을 때를 본다:
  · 단위: 인식기 만들기·크롭 규격, 칸의 표(generic 의 빈 칸 "", usage 의 NULL), machine_final, 쪽 안 검산, _state, 표기
  · 핸들러: 빈 그림 + 잉크를 정해 준 칸 + 답을 정해 주는 가짜 백엔드로 generic·usage 의 load 를 한 줄씩
  · 파이프라인: 합성 가동 일보(usage_synth — 3일, 인쇄 층, 판 B)를 run --answers + by_format 넷으로 (oracle — CER 0, 쪽 안 검산,
    연속성, 대기열, 엑셀, 리포트, info), 종이에 적힌 글자를 돌려주는 가짜 백엔드로 첫날을 (점으로 쓴 시각)
  · slow: 6일치 (--usage-logs --print-layers, 명령으로), V2, 다시 처리의 불변식 흔들기에 형식 칸을 읽는 선택 (test_review_store)
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import clone_db, replay_classify, review_usage
from minedocscan.cli import main
from minedocscan.config import ConfigError, Settings, load_settings
from minedocscan.correct import get_corrector
from minedocscan.evaluate.fields import evaluate_fields, machine_text
from minedocscan.forms.formats import CLOCK_MAX_HOUR, SPELLINGS_MAX, normalize, same_value, spellings
from minedocscan.forms.sitepack import SitePack
from minedocscan.forms.template import meter_slot
from minedocscan.handlers.base import FormHandler, PageContext, format_row, reads_format
from minedocscan.handlers.usage import GATE_REASONS, UNKNOWN, UsageHandler, _state, page_gate
from minedocscan.imaging.cells import observe_cells
from minedocscan.imaging.cropspec import DEFAULT_SPEC, CropSpec
from minedocscan.pipeline import Pipeline
from minedocscan.recognize import (
    READ_FORMATS,
    ByKindRecognizer,
    NullRecognizer,
    OracleRecognizer,
    Recognition,
    build_recognizer,
    by_formats,
    load_answers_json,
    spec_for,
)
from minedocscan.report import build_report, format_report
from minedocscan.review.export import inked_of
from minedocscan.review.queue import build_queue
from minedocscan.review.store import Review, save
from minedocscan.store.db import open_db
from minedocscan.tools import synth_usage
from minedocscan.tools.synth import generate
from minedocscan.tools.synth_usage import T_LOADER
from minedocscan.validate.usage import TOLERANCE, total_check

ALL = list(READ_FORMATS)


# ── 쓸 수 있는 표기 (4.4) ─────────────────────────────────────────────────────
@pytest.mark.parametrize("fmt, value", [
    ("decimal", "1234.5"), ("reading", "1234.5"), ("reading", "08:00"), ("reading", "24:00"), ("time", "08:00"),
    ("time", "00:05"), ("time", "17:30"), ("time", "24:00"), ("time_range", "08:00~17:00"), ("time_range", "22:30~06:00"),
    ("time_range", "08:00~09:00"), ("integer", "7"), (None, "oil leak"), ("reading", ""), ("time_range", "")])
def test_every_spelling_is_the_same_value(fmt, value):
    sp = spellings(fmt, value)
    assert sp[0] == value and len(sp) == len(set(sp)) and len(sp) <= SPELLINGS_MAX
    assert all(same_value(fmt, w, value) for w in sp), [w for w in sp if not same_value(fmt, w, value)]


def test_spellings_are_what_people_write():
    assert spellings("time", "08:00") == ["08:00", "8:00", "08.00", "8.00", "0800", "800", "08", "8"]
    assert spellings("time", "17:30") == ["17:30", "17.30", "1730"]
    assert spellings("reading", "08:00") == ["08:00", "8:00", "08.00", "8.00"]      # 점은 사람이 시각으로 정한 것
    assert spellings("reading", "1234.5") == spellings("decimal", "1234.5") == ["1234.5"]
    assert len(spellings("time_range", "08:00~09:00")) == SPELLINGS_MAX == 128      # 양쪽 8 × 8 × ~·-
    assert {"8-17", "08:00~17:00", "8~17", "0800-1700"} <= set(spellings("time_range", "08:00~17:00"))


def test_same_value_forgives_the_dotted_clock_only_in_a_reading_cell():
    assert same_value("reading", "08.00", "08:00") and same_value("reading", "8.00", "08:00")
    assert not same_value("reading", "8.0", "08:00")                  # 한 자리로 잘못 읽은 시각 — 쪽 안 검산의 ⑤가 막는다
    assert not same_value("decimal", "08.00", "08:00") and not same_value("reading", "", "08:00")
    assert same_value("time_range", "8-17", "08:00~17:00") and same_value("reading", "", "")
    # 평가의 기계 값만 봐준다 (machine_text) — 사람이 넣은 최종 값의 8.00 은 정규화 그대로 견준다 (fields.py)
    assert machine_text("reading", "08.00", "08:00") == "08:00"
    assert machine_text("reading", "8.0", "08:00") == "8.0" and machine_text("decimal", "08.00", "08:00") == "8.00"
    assert machine_text("reading", "", "") == "" and machine_text(None, "oil", "oil") == "oil"


def paper_plans(truth: dict) -> dict[str, synth_usage.PagePlan]:
    """합성 가동 일보의 쪽 계획(종이에 적은 글자 — PagePlan.meter·shifts)을 같은 씨앗으로 다시 만든다 (truth.json 에 더하면 합성의
    바이트가 바뀐다). 쪽의 출처(source) → 계획. 판을 섞는 난수는 따로라 내용은 같다 (synth_usage.assign_variants)."""
    d0 = date.fromisoformat(truth["start"])
    days = [(d0 + timedelta(days=d)).isoformat() for d in range(truth["n_days"])]
    plans = synth_usage.plan_days(days, np.random.default_rng([truth["seed"], 5005]))
    by_key = {}
    for day, ps in zip(days, plans, strict=True):
        for p in ps:
            by_key[(day, p.equipment, p.operator)] = p
    assert len(by_key) == sum(len(ps) for ps in plans)
    return {t["source"]: by_key[(t["date"], t["equipment"], t["operator"])] for t in truth["usage"]}


def test_paper_glyphs_of_the_synthetic_usage_logs_are_spellings(usage_synth):
    plans = paper_plans(usage_synth.truth)
    dotted = 0
    for t in usage_synth.truth["usage"]:
        p = plans[t["source"]]
        assert {k: t[f"meter_{k}"] for k in ("start", "end", "total") if t[f"meter_{k}"] is not None} == \
            {k: float(v) for k, v in p.truth_meter.items() if ":" not in v}                # 같은 계획이다
        for slot, glyph in p.meter.items():
            assert glyph in spellings("reading", p.truth_meter[slot]), (t["source"], slot)
            dotted += glyph != p.truth_meter[slot] and ":" in p.truth_meter[slot]
        for k, (glyph, value) in p.shifts.items():
            assert glyph in spellings("time_range", value), (t["source"], k)
    assert dotted                                                       # 점으로 쓴 시각이 있다 (PUMP)


# ── 인식기: 형식의 표 (4.1) ───────────────────────────────────────────────────
def test_by_format_wraps_and_only_the_listed_formats_are_read(tmp_path):
    plain = build_recognizer(Settings())
    assert isinstance(plain, NullRecognizer) and not any(reads_format(plain, f) for f in ALL)
    assert not reads_format(OracleRecognizer(), "reading")              # 감싸지 않은 oracle 은 형식 칸을 읽지 않는다 (run --answers)
    rec = build_recognizer(Settings(recognizer_by_format={"reading": "oracle", "time_range": "oracle"}))
    assert isinstance(rec, ByKindRecognizer) and rec.describe()["by_format"] == {"reading": "oracle", "time_range": "oracle"}
    assert [f for f in ALL if reads_format(rec, f)] == ["time_range", "reading"]
    assert not reads_format(rec, "integer") and not reads_format(rec, None)
    assert rec.backend_for("handwritten_number").name == "null"         # 한 인자 호출 (info) 은 그대로
    assert rec.backend_for("handwritten_text", "reading").name == "oracle"
    assert rec.backend_for("handwritten_number", "integer").name == "null"
    # by_kind 만이면 형식의 표는 비었다 — 지금과 같다
    assert build_recognizer(Settings(recognizer_by_kind={"handwritten_text": "oracle"})).by_format == {}
    for bad, msg in [({"integer": "oracle"}, "by_kind"), ({"date": "oracle"}, "형식은"),
                     ({"reading": "null"}, "null 는 소수·시각을 읽지 않습니다")]:
        with pytest.raises(ValueError, match=msg):
            build_recognizer(Settings(recognizer_by_format=bad))
    oracle = OracleRecognizer()
    assert by_formats(oracle, []) is oracle and by_formats(oracle, None) is oracle
    assert reads_format(by_formats(oracle, ["time"]), "time") and not reads_format(by_formats(oracle, ["time"]), "reading")
    with pytest.raises(ValueError, match="by_kind"):
        by_formats(oracle, ["integer"])


def test_digits_does_not_read_decimals_or_times(tmp_path):
    from pathlib import Path

    model = Path(__file__).parent / "fixtures" / "digits-fixture"
    st = Settings(recognizer_by_kind={"handwritten_number": "digits"}, recognizer_options={"digits": {"model": str(model)}},
                  recognizer_by_format={"reading": "digits"})
    with pytest.raises(ValueError, match="digits 는 소수·시각을 읽지 않습니다"):
        build_recognizer(st)
    # 모델을 꽂기 전에도 같은 말 — 모델이 없다는 말이 먼저 나오지 않는다 (팩토리가 형식을 미리 말한다)
    with pytest.raises(ValueError, match="digits 는 소수·시각을 읽지 않습니다"):
        build_recognizer(Settings(recognizer_by_format={"reading": "digits"}))
    assert build_recognizer(replace(st, recognizer_by_format={})).by_format == {}


def test_config_reads_the_by_format_table(tmp_path):
    cfg = tmp_path / "minedocscan.toml"
    cfg.write_text('[recognize]\nbackend = "null"\n[recognize.by_format]\nreading = "oracle"\n'
                   '[recognize.digits]\nmodel = "digits-v1"\n', encoding="utf-8")
    s = load_settings(cfg)
    assert s.recognizer_by_format == {"reading": "oracle"} and set(s.recognizer_options) == {"digits"}
    assert load_settings(tmp_path / "없는.toml").recognizer_by_format == {}
    cfg.write_text("[recognize.by_format]\nreading = 3\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="by_format"):
        load_settings(cfg)


class _Spec:
    """형식마다 다른 크롭 규격을 선언하는 백엔드 (형식을 받는 crop_spec_for)."""
    name, formats = "spec", ("reading",)

    def crop_spec_for(self, kind, fmt=None):
        return CropSpec("source", 2.0, None) if fmt == "reading" else None

    def recognize(self, crops, contexts):
        return [Recognition("", 0.0, [], self.name) for _ in contexts]


class _OneArg:
    """종류만 받는 crop_spec_for (숫자 모델처럼) — 형식을 넘기지 않는다."""
    name = "onearg"

    def crop_spec_for(self, kind):
        return CropSpec("source", 1.5, None) if kind == "handwritten_number" else None

    def recognize(self, crops, contexts):
        return [Recognition("", 0.0, [], self.name) for _ in contexts]


def test_crop_spec_is_the_one_of_the_backend_that_reads_the_cell():
    rec = ByKindRecognizer(NullRecognizer(), {"handwritten_number": _OneArg()}, {"reading": _Spec()})
    assert spec_for(rec, "handwritten_number", "reading") == CropSpec("source", 2.0, None)
    assert spec_for(rec, "handwritten_number", "integer") == CropSpec("source", 1.5, None)
    assert spec_for(rec, "handwritten_number") == CropSpec("source", 1.5, None)
    assert spec_for(rec, "handwritten_text", "time") == DEFAULT_SPEC
    assert spec_for(_OneArg(), "handwritten_number", "reading") == CropSpec("source", 1.5, None)
    assert spec_for(NullRecognizer(), "handwritten_number", "reading") == DEFAULT_SPEC


# ── 칸의 표 (4.2) ──────────────────────────────────────────────────────────
def _cell(fmt: str, name: str = "range", region: str = "shifts", row_key: str = "am"):
    cell = SimpleNamespace(region=region, row=0, name=name, kind="handwritten_number", fmt=fmt, row_key=row_key,
                           bbox=(0, 0, 10, 10))
    return SimpleNamespace(cell=cell, ink=0.2)


def _ctx(conf: float = 0.9):
    return SimpleNamespace(page_id="p1", settings=Settings(auto_accept_conf=conf))


# (답, 글자, 신뢰도, 형식, 기준) → (has_value_raw, 상태, 값 — 빈 칸이면 핸들러의 빈 칸)
ROWS = [
    ("empty", "", 0.95, "time_range", None, (0, "auto", "EMPTY")),
    ("empty", "", 0.50, "time_range", None, (1, "pending", None)),
    ("reject", "?", 0.99, "time_range", None, (1, "pending", None)),
    ("value", "08.00", 0.99, "reading", None, (1, "pending", None)),          # 점으로 쓴 시각 — 늘 사람
    ("value", "17.00", 0.99, "reading", None, (1, "pending", None)),
    ("value", "24.00", 0.99, "reading", None, (1, "pending", None)),
    ("value", "08.75", 0.99, "reading", None, (1, "auto", "8.75")),           # 분이 59 를 넘으면 시각이 아니다
    ("value", "01234.5", 0.99, "reading", None, (1, "auto", "1234.5")),
    ("value", "08:00", 0.99, "reading", None, (1, "auto", "08:00")),
    ("value", "8-12", 0.99, "time_range", None, (1, "auto", "08:00~12:00")),
    ("value", "8-12", 0.50, "time_range", None, (1, "pending", None)),
    ("value", "8-", 0.99, "time_range", None, (1, "pending", None)),          # 형식에 맞지 않는 글자
    ("value", "8-12", 0.60, "time_range", 0.5, (1, "auto", "08:00~12:00")),   # 백엔드의 기준
    ("value", "1.5", 0.99, "decimal", None, (1, "auto", "1.5")),
    ("value", "0800", 0.99, "time", None, (1, "auto", "08:00")),
    ("value", "08.00", 0.99, "time", None, (1, "auto", "08:00")),             # time 칸의 점은 시각이다 (계기 칸이 아니다)
    (None, "08:00~12:00", 1.0, "time_range", None, (1, "auto", "08:00~12:00")),  # oracle: 답의 종류를 말하지 않는다
    (None, "", 0.0, "time_range", None, (1, "pending", None)),                # oracle 의 빈 답 — 검수 대기
]


@pytest.mark.parametrize("answer, text, conf, fmt, threshold, want", ROWS)
@pytest.mark.parametrize("handler, empty", [(FormHandler(), ""), (UsageHandler(), None)])
def test_format_row_table(answer, text, conf, fmt, threshold, want, handler, empty):
    o = _cell(fmt)
    r = Recognition(text, conf, [text], "fake", answer=answer, threshold=threshold)
    row = format_row(_ctx(), o, r, empty_final=empty)
    has, status, final = want
    final = empty if final == "EMPTY" else final
    assert (row["has_value_raw"], row["review_status"], row["status_raw"], row["value_final"]) == (has, status, status, final)
    assert (row["value_raw"], row["confidence"], row["backend"], json.loads(row["candidates"])) == (text, conf, "fake", [text])
    assert handler.machine_final(row) == row["value_final"]            # 검수를 다시 덮기 전의 기계 상태 = load 가 만든 것


def _old_machine_final(handler, row):
    """main 의 machine_final (tasks/0011 전) — 설정하지 않은 상태의 행에서 같은 값이 나와야 한다."""
    if isinstance(handler, UsageHandler):
        if row["region"] == "fields" or row["format"] is None:
            return row["value_raw"]
        if row["format"] == "integer":
            return None if not str(row["value_raw"] or "").isdigit() else str(int(row["value_raw"]))
        return None
    return row["value_raw"]


@pytest.mark.parametrize("region", ["shifts", "fields"])
@pytest.mark.parametrize("fmt", ALL)
def test_machine_final_of_unread_rows_is_unchanged(region, fmt):
    """읽지 않은 형식 칸(unread_row — 잉크 있음 + 검수 대기, 기계 값 없음)과 잉크 없는 칸 (generic 은 "", usage 의 표 칸은 NULL)."""
    base = {"region": region, "format": fmt, "has_value_raw": 1, "value_raw": None, "value_final": None,
            "status_raw": "pending", "review_status": "pending", "backend": "ink"}
    unread = dict(base)
    empty_generic = dict(base, has_value_raw=0, value_raw="", value_final="", status_raw="auto", review_status="auto")
    empty_usage = dict(empty_generic, value_final=None if region != "fields" else "")
    for handler, empty in ((FormHandler(), empty_generic), (UsageHandler(), empty_usage)):
        for row in (unread, empty):
            assert handler.machine_final(row) == _old_machine_final(handler, row) == row["value_final"]


@pytest.mark.parametrize("fmt", ALL)
@pytest.mark.parametrize("status", ["auto", "pending"])
def test_machine_final_of_meta_read_fields_is_the_machine_text(fmt, status):
    """표 밖 필드는 형식이 있어도 형식의 길로 읽지 않는다 — 메타 모델이 읽은 필드(_meta_field_row)의 value_final 은 value_raw 그대로
    (main 과 같다: 검수를 다시 덮기 전의 기계 상태가 load 와 같아야 다시 처리의 불변식이 선다)."""
    row = {"region": "fields", "format": fmt, "has_value_raw": 1, "value_raw": "0800", "value_final": "0800",
           "status_raw": status, "review_status": status, "backend": "meta-digits"}
    for handler in (FormHandler(), UsageHandler()):
        assert handler.machine_final(row) == _old_machine_final(handler, row) == "0800"


def test_pending_format_cells_are_unknown_to_the_usage_record():
    """검수 대기인 형식 칸은 값이 있어도 모른다 (자동 적재한 칸에 사람이 '읽을 수 없음' — apply_verdict 는 값을 그대로 둔다)."""
    row = {"review_status": "pending", "value_final": "1234.5", "format": "reading", "has_value": 1}
    assert _state(row) is UNKNOWN
    assert _state(dict(row, review_status="auto")) == "1234.5"
    assert _state(dict(row, review_status="reviewed", value_final="1234.50")) == "1234.50"
    assert _state(dict(row, format="integer", value_final="7")) == "7"            # 정수 칸은 그대로 (검수 대기여도 기계 값)
    assert _state(dict(row, value_final=None)) is UNKNOWN


# ── 쪽 안 검산 (4.2) ────────────────────────────────────────────────────────
GATE_TPL = SimpleNamespace(regions=[{"name": "meter", "role": "meter"}, {"name": "shifts", "role": "shifts"},
                                    {"name": "tally", "role": "tally"}])


def _m(slot, value, status="auto", fmt="reading"):
    """계기 칸의 기계 행. value None = 빈 칸으로 자동 적재 (has 0)."""
    has = value is not None
    return {"region": "meter", "kind": "handwritten_number", "format": fmt, "field_name": slot, "row_key": "reading",
            "has_value_raw": int(has), "value_final": None if status == "pending" else value, "review_status": status,
            "status_raw": status, "field_id": f"m:{slot}"}


def _s(key, value, status="auto"):
    has = value is not None
    return {"region": "shifts", "kind": "handwritten_number", "format": "time_range", "field_name": "range", "row_key": key,
            "has_value_raw": int(has), "value_final": None if status == "pending" else value, "review_status": status,
            "status_raw": status, "field_id": f"s:{key}"}


GOOD_SHIFTS = [_s("am", "08:00~12:00"), _s("pm", "13:00~17:00"), _s("ot", None)]

GATE = [
    ("good", [_m("start", "1234.5"), _m("end", "1240.0"), _m("total", None)], None),
    ("good with total", [_m("start", "1234.5"), _m("end", "1240.0"), _m("total", "5.5")], None),
    ("total at the rounding edge", [_m("start", "1234.5"), _m("end", "1240.0"), _m("total", "5.45")], None),
    ("start > end", [_m("start", "1240.0"), _m("end", "1234.5"), _m("total", None)], "range"),
    ("26 hours", [_m("start", "1234.0"), _m("end", "1260.0"), _m("total", None)], "range"),
    ("total off", [_m("start", "1234.5"), _m("end", "1240.0"), _m("total", "5.4")], "total"),
    ("clock as a total", [_m("start", "1234.5"), _m("end", "1240.0"), _m("total", "05:30")], "total"),
    ("mixed", [_m("start", "1234.5"), _m("end", "17:00"), _m("total", None)], "mixed"),
    ("start only", [_m("start", "1234.5"), _m("end", None), _m("total", None)], "incomplete"),
    ("total only", [_m("start", None), _m("end", None), _m("total", "5.5")], "incomplete"),
    ("one pending", [_m("start", "1234.5"), _m("end", "1240.0", "pending"), _m("total", None)], "pending"),
    ("both under 24", [_m("start", "8.0"), _m("end", "17.0"), _m("total", None)], "clock_like"),
    ("24 is a clock hour", [_m("start", "20.0"), _m("end", "24.0"), _m("total", None)], "clock_like"),
    ("clock page", [_m("start", "08:00"), _m("end", "17:00"), _m("total", None)], None),
    ("clock page with a total", [_m("start", "08:00"), _m("end", "17:30"), _m("total", "9.5")], None),
    ("clock page, total off", [_m("start", "08:00"), _m("end", "17:00"), _m("total", "8.5")], "total"),
    ("clock page, clock total", [_m("start", "08:00"), _m("end", "17:00"), _m("total", "09:00")], "total"),
    ("all empty", [_m("start", None), _m("end", None), _m("total", None)], None),
    ("decimal and time formats", [_m("start", "1234.5", fmt="decimal"), _m("end", "1240.0", fmt="decimal")], None),
]


@pytest.mark.parametrize("name, meter, reason", GATE, ids=[g[0] for g in GATE])
def test_meter_gate(name, meter, reason):
    rows = meter + GOOD_SHIFTS
    out, counts = page_gate(GATE_TPL, rows)
    values = [r for r in meter if r["review_status"] == "auto" and r["has_value_raw"]]
    assert set(counts) == set(GATE_REASONS)
    assert counts == {k: (len(values) if k == reason else 0) for k in GATE_REASONS}
    for before, after in zip(rows, out, strict=True):
        demoted = reason is not None and before in values
        if demoted:
            assert (after["review_status"], after["status_raw"], after["value_final"]) == ("pending", "pending", None)
            assert after["has_value_raw"] == 1
        else:
            assert after == before                                     # 빈 칸으로 자동 적재한 칸, 근무 시각 표는 그대로
    assert rows == meter + GOOD_SHIFTS                                  # 받은 행은 고치지 않는다


def test_meter_gate_total_is_the_page_check_function():
    """총 = 종료 − 시작: 쪽 안 검산(page_checks)과 같은 함수 — 반올림 경계에서도 같은 답."""
    for s in (0.1, 1234.5, 99.95):
        for span in (0.2, 5.5, 7.3):
            e = round(s + span, 4)
            for d in (-0.06, -0.05, -0.0499, 0.0, 0.05, 0.0501, 0.1):
                t = round(span + d, 4)
                old = abs(round(t - (e - s), 4)) <= TOLERANCE                # main 의 page_checks
                assert total_check(s, e, t)[2] == old
                if s > CLOCK_MAX_HOUR:
                    rows = [_m("start", str(s)), _m("end", str(e)), _m("total", str(t))]
                    assert (page_gate(GATE_TPL, rows)[1]["total"] == 0) == old, (s, e, t)


@pytest.mark.parametrize("shifts, reason", [
    ([_s("am", "08:00~20:00"), _s("pm", "20:00~09:00"), _s("ot", None)], "shift_day"),     # 25시간
    ([_s("am", "08:00~20:00"), _s("pm", "20:00~08:00"), _s("ot", None)], None),            # 24시간 — 하루의 상한까지
    ([_s("am", "08:00~12:00"), _s("pm", None, "pending"), _s("ot", None)], "shift_pending"),
    ([_s("am", None), _s("pm", None), _s("ot", None)], None),
])
def test_shift_gate_and_tables_are_separate(shifts, reason):
    meter = [_m("start", "1234.5"), _m("end", "1240.0"), _m("total", None)]
    out, counts = page_gate(GATE_TPL, meter + shifts)
    assert out[:3] == meter                                             # 계기 표는 근무 시각 표와 따로
    values = [r for r in shifts if r["review_status"] == "auto" and r["has_value_raw"]]
    assert counts == {k: (len(values) if k == reason else 0) for k in GATE_REASONS}
    assert all(r["review_status"] == "pending" for r in out[3:] if r["has_value_raw"]) == (reason is not None or not values)
    # 계기 표가 내려가도 근무 시각 표는 그대로
    out2, _ = page_gate(GATE_TPL, [_m("start", "1240.0"), _m("end", "1234.5")] + GOOD_SHIFTS)
    assert out2[2:] == GOOD_SHIFTS and out2[0]["review_status"] == "pending"


# ── 핸들러: 빈 그림 + 정해 준 잉크 + 가짜 백엔드 ───────────────────────────
class Scripted:
    """(칸 이름, 행 키) → 답. 없는 칸은 신뢰도 0 의 빈 답 (검수 대기)."""
    name = "scripted"
    formats = tuple(READ_FORMATS)

    def __init__(self, answers: dict):
        self.answers = answers
        self.seen: list = []

    def recognize(self, crops, contexts):
        self.seen += [(c.field_name, c.row_key, c.fmt, c.bbox) for c in contexts]
        return [self.answers.get((c.field_name, c.row_key), Recognition("", 0.0, [], self.name, answer="reject"))
                for c in contexts]


def _v(text, conf=0.99):
    return Recognition(text, conf, [text], "scripted", answer="value")


@pytest.fixture(scope="module")
def loader(usage_synth):
    site = SitePack(usage_synth.site)
    tpl = site.templates[T_LOADER]
    blank = np.full(tpl.reference.shape[:2], 255, np.uint8)
    return site, tpl, blank, observe_cells(blank, tpl)


def load_page(loader, answers: dict, inked: set, handler=None, by_format=True):
    """로우더 작업일보 한 쪽을 핸들러로 — 그림은 비었고 inked 의 칸(이름, 행 키)만 잉크가 있다고 본다."""
    site, tpl, blank, obs = loader
    obs = [replace(o, ink=0.3) if (o.cell.name, o.cell.row_key) in inked else o for o in obs]
    backend = Scripted(answers)
    rec = ByKindRecognizer(NullRecognizer(), {}, {f: backend for f in READ_FORMATS}) if by_format else NullRecognizer()
    con = open_db("sqlite:///:memory:")
    ctx = PageContext(con, Settings(auto_accept_conf=0.9), site, tpl, "d1", "d1-p1", 1, "scan_2030-01-07",
                      {"date": "2030-01-07"}, blank, obs, rec, get_corrector("none"))
    summary = (handler or UsageHandler()).load(ctx)
    rows = {(r["field_name"], r["row_key"]): dict(r) for r in con.execute("SELECT * FROM doc_field WHERE format NOT IN "
                                                                         "('integer') AND format IS NOT NULL")}
    usage = con.execute("SELECT * FROM eq_usage_daily").fetchone()
    return SimpleNamespace(summary=summary, rows=rows, usage=None if usage is None else dict(usage), backend=backend, con=con)


METER = {("start", "reading"), ("end", "reading"), ("total", "reading")}
SHIFTS = {("range", "am"), ("range", "pm"), ("range", "ot")}


# 계기의 시작만 자동 적재하면 쪽 안 검산이 내린다 (시작만 — incomplete) — 그 줄은 아래의 시험(test_usage_page_gate_through_the_handler)
THROUGH = [r for r in ROWS if r[3] == "time_range" or (r[3] == "reading" and r[5][1] == "pending")]


@pytest.mark.parametrize("answer, text, conf, fmt, threshold, want", THROUGH)
@pytest.mark.parametrize("generic", [False, True])
def test_the_table_through_the_handlers(loader, answer, text, conf, fmt, threshold, want, generic):
    """칸의 표를 한 줄씩 핸들러로 (generic — 빈 칸 "", 쪽 안 검산 없음 / usage — 빈 칸 NULL). 근무 시각 칸 하나(다른 칸은 비었다 —
    검산을 지나간다), 계기의 시작 칸 하나(점으로 쓴 시각 — 검수 대기라 검산이 내릴 값 칸이 없다)."""
    key = ("start", "reading") if fmt == "reading" else ("range", "am")
    r = Recognition(text, conf, [text], "scripted", answer=answer, threshold=threshold)
    got = load_page(loader, {key: r}, {key}, handler=FormHandler() if generic else None)
    has, status, final = want
    final = ("" if generic else None) if final == "EMPTY" else final
    row = got.rows[key]
    assert (row["has_value_raw"], row["review_status"], row["status_raw"], row["value_final"], row["value_raw"]) == \
        (has, status, status, final, text)
    assert got.backend.seen == [(key[0], key[1], fmt, tuple(row[k] for k in ("x0", "y0", "x1", "y1")))]   # fmt·bbox
    others = [v for k, v in got.rows.items() if k != key]
    assert all((v["has_value_raw"], v["review_status"], v["backend"]) == (0, "auto", "ink") for v in others)
    assert all(v["value_final"] == ("" if generic else None) for v in others)
    assert ("format_gate_total" in got.summary) == (not generic)       # 쪽 안 검산은 가동 일보의 역할이 있는 표에서만


def test_usage_page_gate_through_the_handler(loader):
    good = {("start", "reading"): _v("1234.5"), ("end", "reading"): _v("1240.0"),
            ("range", "am"): _v("08:00~12:00"), ("range", "pm"): _v("13:00~17:00")}
    inked = set(good)
    ok = load_page(loader, good, inked)
    assert all(ok.rows[k]["review_status"] == "auto" for k in good) and ok.rows[("total", "reading")]["has_value_raw"] == 0
    assert (ok.usage["reading_kind"], ok.usage["hours"], ok.usage["hours_basis"], ok.usage["review_status"]) == \
        ("meter", 5.5, "meter", "auto")
    assert (ok.usage["meter_start_raw"], ok.usage["shift_minutes"]) == ("1234.5", 480)
    assert {k: v for k, v in ok.summary.items() if k.startswith("format_gate_")} == {f"format_gate_{k}": 0 for k in GATE_REASONS}
    assert ok.con.execute("SELECT result FROM xcheck_usage WHERE check_kind = 'total'").fetchall() == []

    total = load_page(loader, good | {("total", "reading"): _v("5.4")}, inked | {("total", "reading")})
    assert all(total.rows[k]["review_status"] == "pending" and total.rows[k]["value_final"] is None for k in METER)
    assert total.rows[("total", "reading")]["value_raw"] == "5.4"                  # 기계가 읽은 글자는 남는다
    assert all(total.rows[k]["review_status"] == "auto" for k in SHIFTS & set(good))       # 표마다 따로
    assert total.summary["format_gate_total"] == 3 and total.usage["reading_kind"] == "pending" and total.usage["hours"] is None
    assert total.con.execute("SELECT COUNT(*) FROM xcheck_usage WHERE check_kind = 'total'").fetchone()[0] == 0

    day = load_page(loader, good | {("range", "am"): _v("06:00~20:00"), ("range", "pm"): _v("20:00~08:00")}, inked)
    assert day.summary["format_gate_shift_day"] == 2 and all(day.rows[k]["review_status"] == "pending" for k in SHIFTS & inked)
    assert day.usage["hours_basis"] == "meter" and day.usage["shift_minutes"] is None

    dotted = load_page(loader, good | {("start", "reading"): _v("08.00"), ("end", "reading"): _v("17:00")}, inked)
    assert dotted.rows[("start", "reading")]["value_raw"] == "08.00"
    assert all(dotted.rows[k]["review_status"] == "pending" for k in METER & inked)
    assert dotted.summary["format_gate_pending"] == 1 and dotted.usage["reading_kind"] == "pending"

    tenth = load_page(loader, good | {("start", "reading"): _v("8.0"), ("end", "reading"): _v("17.0")}, inked)
    assert tenth.summary["format_gate_clock_like"] == 2 and tenth.usage["hours"] is None

    # 설정하지 않으면: 인식기에 가지 않고, 잉크가 있으면 검수 대기 — 요약에 검산의 키가 없다
    plain = load_page(loader, good, inked, by_format=False)
    assert plain.backend.seen == [] and not any(k.startswith("format_gate_") for k in plain.summary)
    assert all((plain.rows[k]["backend"], plain.rows[k]["review_status"], plain.rows[k]["value_raw"]) == ("ink", "pending", None)
               for k in inked)


def test_inked_of_format_cells():
    """export-crops 의 inked: 형식 칸은 인식기에 갔거나 잉크 판정이 있는 칸 (빈 칸으로 자동 적재한 칸도 검증에 든다)."""
    f = {"format": "reading"}
    assert inked_of(f | {"backend": "readings", "has_value_raw": 0})
    assert inked_of(f | {"backend": "readings", "has_value_raw": 1}) and inked_of(f | {"backend": "ink", "has_value_raw": 1})
    assert not inked_of(f | {"backend": "ink", "has_value_raw": 0})
    for fmt in (None, "integer"):                                       # 정수·글자 칸은 그대로 — 인식기에 간 칸
        assert inked_of({"format": fmt, "backend": "digits", "has_value_raw": 0})
        assert not inked_of({"format": fmt, "backend": "ink", "has_value_raw": 1})


# ── 파이프라인: run --answers + by_format (oracle) ───────────────────────────
def _config(root, formats=ALL, reviews=None) -> str:
    lines = ["[pipeline]", "save_aligned = false", "[paths]",
             f"reviews = {json.dumps(str(reviews or root / 'reviews.jsonl'))}", "[recognize.by_format]"]
    lines += [f'{f} = "oracle"' for f in formats]
    cfg = root / "minedocscan.toml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(cfg)


def run_formats(synth, root) -> dict:
    """run --answers + [recognize.by_format] 넷 (명령으로 — cli 의 감싸기를 지난다)."""
    cfg = _config(root)
    args = ["run", "--json", "--config", cfg, "--answers", str(synth.answers_path), "--site", str(synth.site),
            "--archive-root", str(synth.scans), "--work-root", str(root / "work")]
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(args) == 0
    out = json.loads(buf.getvalue())
    con = open_db(f"sqlite:///{root / 'work' / 'minedocscan.db'}")
    st = replace(load_settings(cfg), site=synth.site, archive_root=synth.scans, work_root=root / "work")
    return {"con": con, "site": SitePack(synth.site), "settings": st, "out": out, "synth": synth, "cfg": cfg,
            "answers": load_answers_json(synth.answers_path), "root": root}


@pytest.fixture(scope="module")
def formats_run(usage_synth, usage_run, tmp_path_factory) -> dict:
    """usage_synth 를 oracle + by_format 넷으로 (run --answers 가 만드는 인식기와 같다 — by_formats). 분류는 usage_run 의 것을 다시
    쓴다 (시간 — 분류는 인식기와 상관없다). 명령으로 돌리는 것은 slow 의 6일치 (run_formats)."""
    root = tmp_path_factory.mktemp("formats_run")
    answers = load_answers_json(usage_synth.answers_path)
    cfg = _config(root)
    st = replace(load_settings(cfg), site=usage_synth.site, archive_root=usage_synth.scans, work_root=root / "work")
    pipe = Pipeline(st, recognizer=by_formats(OracleRecognizer(answers, site=SitePack(usage_synth.site)), ALL))
    with pytest.MonkeyPatch.context() as mp:
        replay_classify(pipe, usage_run, mp)
        pipe.run([usage_synth.scans])
    return {"con": pipe.con, "site": pipe.site, "settings": st, "out": {"run": pipe.summary}, "synth": usage_synth,
            "cfg": cfg, "answers": answers, "root": root}


def by_source(con) -> dict[str, dict]:
    return {r["source"]: dict(r) for r in con.execute(
        "SELECT u.*, d.source_name || '#' || p.page_no AS source FROM eq_usage_daily u JOIN doc_page p ON u.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id")}


def meter_cells(con, page_id) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM doc_field WHERE page_id = ? AND region = 'meter' "
                                         "AND kind LIKE 'handwritten%'", (page_id,))]


def assert_oracle_reads_format_cells(run) -> None:
    """oracle + by_format 넷: 형식 칸의 기계 값 CER 0, 총이 어긋난 쪽(TRUCK_total_mismatch)의 계기 칸은 전부 검수 대기(값 NULL),
    그 밖의 쪽은 자동 적재이고 가동 기록이 정답과 같다. 인쇄된 ~ 만 있는 칸은 인식기에 가지 않았다 (인쇄 층)."""
    con, truth, site = run["con"], run["synth"].truth["usage"], run["site"]
    ev = evaluate_fields(con, run["answers"], target="raw", site=site)
    groups = {k: g for k, g in ev["by_field_kind"].items() if k.rsplit("/", 1)[-1] in READ_FORMATS}
    assert {k.rsplit("/", 1)[-1] for k in groups} == {"reading", "time_range"}
    assert all(g["cer"] == 0 and g["field_accuracy"] == 1 for g in groups.values()), groups
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE format IN ('reading', 'time_range') AND backend = 'oracle' "
                       "AND value_raw = ''").fetchone()[0] == 0
    rows = by_source(con)
    mismatch = [t for t in truth if "TRUCK_total_mismatch" in t["scenarios"]]
    assert mismatch
    for t in truth:
        u = rows[t["source"]]
        cells = [c for c in meter_cells(con, u["page_id"]) if c["has_value_raw"]]
        if t in mismatch:
            assert len(cells) == 3 and all((c["status_raw"], c["review_status"], c["value_final"]) == ("pending", "pending", None)
                                           for c in cells), t["source"]
            assert (u["reading_kind"], u["hours"], u["review_status"]) == ("pending", None, "pending")
            continue
        assert all(c["status_raw"] == "auto" for c in cells) and u["review_status"] == "auto", t["source"]
        for k in ("reading_kind", "meter_start", "meter_end", "meter_total", "clock_start", "clock_end", "shift_minutes",
                  "hours_basis"):
            assert u[k] == t[k], (t["source"], k)
        assert u["hours"] == pytest.approx(t["hours"]) if t["hours"] is not None else u["hours"] is None
    want_total = sum(3 for _t in mismatch)
    assert run["out"]["run"]["handlers"]["usage"]["format_gate_total"] == want_total
    assert sum(v for k, v in run["out"]["run"]["handlers"]["usage"].items() if k.startswith("format_gate_")) == want_total
    # 자동 적재된 쪽의 총 검산은 어긋날 수 없다
    assert con.execute("SELECT COUNT(*) FROM xcheck_usage WHERE check_kind = 'total' AND result = 'mismatch'").fetchone()[0] == 0


def test_oracle_reads_format_cells_and_the_gate_holds_mismatched_totals(formats_run):
    assert_oracle_reads_format_cells(formats_run)


def test_wrong_start_is_auto_and_shows_in_usage_check(formats_run, tmp_path):
    """잘못 쓴 시작을 바르게 읽으면 자동 적재 — 연속성의 overlap 으로 보인다 (ADR 0006). 장비명은 사람이 (표 밖 필드).
    합성 계획에서 잘못 쓴 시작의 앞 기록은 늘 총이 어긋난 쪽이다 (d % 3 == 1 → 2) — 그 쪽의 계기는 검수 대기라 기계만으로는
    연속성을 모른다(unknown). 사람이 readings 대기열의 그 쪽을 넣으면 overlap 이 된다."""
    con = clone_db(formats_run["con"])
    st = replace(formats_run["settings"], reviews=tmp_path / "reviews.jsonl")
    site, answers = formats_run["site"], formats_run["answers"]
    review_usage(con, site, st, answers, regions={"fields"})
    truth = formats_run["synth"].truth["usage"]
    wrong = [t for t in truth if "TRUCK_wrong_start" in t["scenarios"]]
    rows = by_source(con)
    assert wrong
    for t in wrong:
        c = con.execute("SELECT result FROM xcheck_usage WHERE page_id = ? AND check_kind = 'continuity'",
                        (rows[t["source"]]["page_id"],)).fetchone()
        assert c["result"] == "unknown"                                # 앞 기록(총이 어긋난 쪽)의 종료를 모른다
    items = build_queue(con, "readings", site=site)["items"]
    keys = {r["field_id"]: r for r in con.execute(
        "SELECT f.field_id, d.source_name || '#' || p.page_no AS source, p.template_name, f.region, f.field_name, f.row_key "
        "FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id")}
    for it in items:
        for cell in it["cells"]:
            k = keys[cell["field_id"]]
            text = answers.get((k["source"], k["template_name"], k["region"], k["field_name"], k["row_key"] or ""))
            save(con, site, st, Review(cell["field_id"], "value", text, "jp") if text
                 else Review(cell["field_id"], "empty", reviewer="jp"))
    assert build_queue(con, "readings", site=site)["done"] == len(items) > 0
    rows = by_source(con)
    items = {it["item_id"]: it for it in build_queue(con, "usage-check", site=site)["items"]}
    for t in wrong:
        u = rows[t["source"]]
        assert u["review_status"] == "auto" and u["meter_start"] == t["meter_start"]
        c = con.execute("SELECT * FROM xcheck_usage WHERE page_id = ? AND check_kind = 'continuity'", (u["page_id"],)).fetchone()
        assert c["result"] == "overlap"
        item = items[f"usage-check:{u['page_id']}:continuity:{c['item']}"]
        cell = next(x for x in item["cells"] if x["field_id"] == u["start_field_id"])
        assert cell["current"]["value"] == str(t["meter_start"])          # 지금의 최종 값 = 기계가 읽은 값


def _readings_pages(con, site, audit=None) -> list[str]:
    return sorted(it["item_id"].split(":", 1)[1] for it in build_queue(con, "readings", site=site, audit=audit)["items"])


def test_readings_queue_shows_only_pages_the_machine_sent(formats_run, usage_run):
    """읽지 않을 때: 잉크가 있는 칸의 쪽 (지금과 같은 쪽·같은 수). 읽을 때: 기계가 검수로 보낸 칸이 있는 쪽 (자동 적재된 쪽은 없다).
    --audit 은 기계의 상태와 상관없이 같은 쪽."""
    plain_con, site = usage_run["pipe"].con, usage_run["pipe"].site

    def cells(con):
        tpls = {t.name: t for t in site.templates.values() if t.handler == "usage"}
        out: dict[str, list] = {}
        for r in con.execute("SELECT f.*, p.template_name FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                             "WHERE f.kind LIKE 'handwritten%'"):
            tpl = tpls.get(r["template_name"])
            role = None if tpl is None else next((g.get("role") for g in tpl.regions if g["name"] == r["region"]), None)
            if (role == "meter" and meter_slot(r["field_name"], r["row_key"])) or \
                    (role == "shifts" and r["format"] == "time_range"):
                out.setdefault(r["page_id"], []).append(r)
        return out

    plain = cells(plain_con)
    want_plain = sorted(p for p, rs in plain.items() if any(r["has_value_raw"] for r in rs))
    assert _readings_pages(plain_con, site) == want_plain
    assert want_plain == sorted(p for p, rs in plain.items() if any(r["status_raw"] == "pending" for r in rs))
    con = formats_run["con"]
    sent = sorted(p for p, rs in cells(con).items() if any(r["status_raw"] == "pending" for r in rs))
    mismatch = {by_source(con)[t["source"]]["page_id"] for t in formats_run["synth"].truth["usage"]
                if "TRUCK_total_mismatch" in t["scenarios"]}
    assert _readings_pages(con, formats_run["site"]) == sent == sorted(mismatch)
    assert len(sent) < len(want_plain)
    assert _readings_pages(con, formats_run["site"], audit=5) == _readings_pages(plain_con, site, audit=5)


def test_illegible_on_an_auto_meter_cell_makes_the_hours_unknown(formats_run, tmp_path):
    con = clone_db(formats_run["con"])
    st = replace(formats_run["settings"], reviews=tmp_path / "reviews.jsonl")
    site = formats_run["site"]
    u = next(r for r in by_source(con).values() if r["hours_basis"] == "meter" and r["review_status"] == "auto")
    start = con.execute("SELECT * FROM doc_field WHERE field_id = ?", (u["start_field_id"],)).fetchone()
    machine = start["value_final"]
    for verdict, value, kind, hours in [("illegible", "", "pending", None), ("value", "1.5", None, None),
                                        ("illegible", "", "pending", None)]:
        save(con, site, st, Review(start["field_id"], verdict, value, "jp"))
        f = con.execute("SELECT * FROM doc_field WHERE field_id = ?", (start["field_id"],)).fetchone()
        now = con.execute("SELECT * FROM eq_usage_daily WHERE page_id = ?", (u["page_id"],)).fetchone()
        if verdict == "illegible":                     # 기계 값은 남지만 (apply_verdict) 가동 시간은 모른다
            assert (f["review_status"], f["value_final"], f["status_raw"]) == ("pending", machine, "auto")
            assert (now["reading_kind"], now["hours"], now["hours_basis"]) == (kind, hours, None)
        else:
            assert (f["review_status"], f["value_final"]) == ("reviewed", "1.5") and now["reading_kind"] == "meter"


def test_excel_shows_auto_meters_as_values_and_pending_as_unsure(formats_run):
    from test_export_excel import book, business_rows, check_business

    con, site = formats_run["con"], formats_run["site"]
    truth = formats_run["synth"].truth["usage"]
    rows = by_source(con)
    from minedocscan.export import labels as L

    seen = set()
    for day in sorted({t["date"] for t in truth}):
        b = book(con, site, day)
        check_business(con, b, day, site)                      # 지금의 규칙 — 확정일 때만 값
        for r in business_rows(b, "usage"):
            t = next(t for t in truth if t["source"] == r[L.SOURCE])
            u = rows[t["source"]]
            if "TRUCK_total_mismatch" in t["scenarios"]:
                assert r["계기 시작"] is None and r["가동 시간"] is None
                seen.add("pending")
            elif u["hours_basis"] == "meter":
                assert (r["계기 시작"], r["계기 종료"]) == (t["meter_start"], t["meter_end"])
                assert r["가동 시간"] == pytest.approx(t["hours"])
                seen.add("auto")
    assert seen == {"pending", "auto"}


def test_report_info_and_the_unset_report(formats_run, usage_run, tmp_path, monkeypatch):
    con = formats_run["con"]
    rep = build_report(con)
    fc = rep["format_cells"]
    want: dict = {}
    for r in con.execute("SELECT format, status_raw, has_value_raw FROM doc_field WHERE format IN ('reading', 'time_range') "
                         "AND backend = 'oracle'"):
        d = want.setdefault(r[0], {"read": 0, "auto_value": 0, "auto_empty": 0, "pending": 0, "dotted": 0})
        d["read"] += 1
        d["pending" if r[1] == "pending" else ("auto_value" if r[2] else "auto_empty")] += 1
    assert fc == want and fc["reading"]["pending"] > 0
    assert "기계가 읽은 형식 칸 [reading]" in format_report(rep)
    assert "format_cells" not in build_report(usage_run["pipe"].con)      # 읽지 않으면 키가 없다 (regress 의 기준 그대로)
    # info: 형식마다 줄 (지금의 종류마다 줄과 같은 꼴). 설정하지 않으면 by_format 이 없다
    monkeypatch.delenv("MINEDOCSCAN_CONFIG", raising=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["info", "--config", formats_run["cfg"]]) == 0
    text = buf.getvalue()
    assert all(f"인식기 [{f}]: oracle" in text for f in ALL) and "인식기 [handwritten_number]: null" in text
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["info", "--json", "--config", formats_run["cfg"]]) == 0
    data = json.loads(buf.getvalue())
    assert data["recognizer"]["by_format"] == {f: {"backend": "oracle"} for f in ALL}
    plain = tmp_path / "plain.toml"
    plain.write_text("[recognize]\nbackend = \"null\"\n", encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["info", "--json", "--config", str(plain)]) == 0
    assert "by_format" not in json.loads(buf.getvalue())["recognizer"]


class Blank:
    """형식 칸을 모두 빈 칸(신뢰도 0.99)으로 읽는 가짜 백엔드 — 기계가 빈 칸으로 자동 적재한 칸(인식기에 갔다, has_value_raw 0)을 만든다."""
    name = "blank"
    formats = tuple(READ_FORMATS)

    def recognize(self, crops, contexts):
        return [Recognition("", 0.99, [], self.name, answer="empty") for _ in contexts]


def test_export_crops_counts_cells_the_machine_emptied(usage_synth, usage_run, tmp_path):
    """4.6: 기계가 빈 칸으로 자동 적재한 형식 칸도 inked (검증에 든다). 지금까지의 규칙(has_value_raw 만)으로는 빠졌다."""
    from minedocscan.review.export import export_crops

    st = Settings(site=usage_synth.site, archive_root=usage_synth.scans, work_root=tmp_path / "work",
                  reviews=tmp_path / "reviews.jsonl", save_aligned=False)
    pipe = Pipeline(st, recognizer=by_formats(Blank(), ALL))
    with pytest.MonkeyPatch.context() as mp:
        replay_classify(pipe, usage_run, mp)
        pipe.run(sorted(usage_synth.scans.glob("*.pdf"))[:1])
    con = pipe.con
    fid = con.execute("SELECT field_id FROM doc_field WHERE format = 'reading' AND backend = 'blank' AND status_raw = 'auto' "
                      "AND has_value_raw = 0 ORDER BY field_id LIMIT 1").fetchone()[0]
    save(con, pipe.site, st, Review(fid, "value", "1234.5", "jp"))
    export_crops(con, pipe.site, st, tmp_path / "crops")
    lines = [json.loads(x) for p in (tmp_path / "crops").glob("*/labels.jsonl") for x in p.read_text(encoding="utf-8").splitlines()]
    line = next(x for x in lines if x["field_id"] == fid)
    assert line["inked"] is True
    assert build_report(con)["format_cells"]["reading"]["auto_empty"] > 0


def test_export_crops_marks_cells_the_machine_read(formats_run, tmp_path):
    from minedocscan.review.export import export_crops

    con = clone_db(formats_run["con"])
    st = replace(formats_run["settings"], reviews=tmp_path / "reviews.jsonl")
    site = formats_run["site"]
    auto = con.execute("SELECT field_id FROM doc_field WHERE format = 'reading' AND backend = 'oracle' AND status_raw = 'auto' "
                       "ORDER BY field_id LIMIT 1").fetchone()[0]
    empty = con.execute("SELECT field_id FROM doc_field WHERE format = 'reading' AND backend = 'ink' ORDER BY field_id "
                        "LIMIT 1").fetchone()[0]
    save(con, site, st, Review(auto, "value", "1.5", "jp"))
    save(con, site, st, Review(empty, "empty", "", "jp"))
    export_crops(con, site, st, tmp_path / "crops")
    lines = {}
    for p in (tmp_path / "crops").glob("*/labels.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            lines[d["field_id"]] = d
    assert lines[auto]["inked"] is True and lines[empty]["inked"] is False


# ── 종이에 적힌 글자를 돌려주는 가짜 백엔드 (점으로 쓴 시각) ─────────────────────
class Paper:
    """합성 계획의 종이 글자(PagePlan.meter·shifts)를 적힌 대로 돌려준다 — 값이 없는 칸은 빈 칸. 신뢰도 0.99."""
    name = "paper"
    formats = tuple(READ_FORMATS)

    def __init__(self, plans: dict):
        self.plans = plans

    def recognize(self, crops, contexts):
        out = []
        for c in contexts:
            p, text = self.plans.get(c.source), None
            if p is not None and c.region == "meter":
                text = p.meter.get(meter_slot(c.field_name, c.row_key))
            elif p is not None and c.region == "shifts":
                text = p.shifts.get(c.row_key, (None, None))[0]
            out.append(Recognition(text, 0.99, [text], self.name, answer="value") if text
                       else Recognition("", 0.99, [], self.name, answer="empty"))
        return out


@pytest.fixture(scope="module")
def paper_run(usage_synth, usage_run, tmp_path_factory) -> dict:
    """usage_synth 의 앞 이틀을 종이 글자로 — 첫날(d % 3 == 0)의 PUMP 는 종료 칸이 점으로 쓴 시각, 둘째 날의 PUMP 는 시작만 점으로 쓰고
    종료는 콜론이다 (칸의 규칙으로는 자동 적재 — 쪽 안 검산이 내린다). 분류는 usage_run 의 것을 다시 쓴다."""
    root = tmp_path_factory.mktemp("paper_run")
    plans = paper_plans(usage_synth.truth)
    st = Settings(site=usage_synth.site, archive_root=usage_synth.scans, work_root=root / "work", reviews=root / "reviews.jsonl",
                  save_aligned=False)
    rec = by_formats(Paper(plans), ALL)
    pipe = Pipeline(st, recognizer=rec)
    pdfs = sorted(usage_synth.scans.glob("*.pdf"))[:2]
    with pytest.MonkeyPatch.context() as mp:
        replay_classify(pipe, usage_run, mp)
        pipe.run(pdfs)
    days = {f.stem.split("_", 1)[1] for f in pdfs}
    return {"pipe": pipe, "plans": plans, "days": days, "settings": st,
            "truth": [t for t in usage_synth.truth["usage"] if t["date"] in days],
            "answers": load_answers_json(usage_synth.answers_path)}


def test_dotted_clocks_wait_for_people_and_hold_their_table(paper_run):
    con, plans = paper_run["pipe"].con, paper_run["plans"]
    rows = by_source(con)
    pumps = [t for t in paper_run["truth"] if t["equipment"] == "PUMP"]
    assert pumps
    mixed = 0
    for t in pumps:
        p, u = plans[t["source"]], rows[t["source"]]
        cells = {meter_slot(c["field_name"], c["row_key"]): c for c in meter_cells(con, u["page_id"])}
        dotted = [s for s, g in p.meter.items() if "." in g and ":" in p.truth_meter[s]]
        assert dotted
        for s, g in p.meter.items():
            c = cells[s]
            assert (c["value_raw"], c["review_status"], c["status_raw"], c["value_final"]) == (g, "pending", "pending", None)
        mixed += any(":" in g for g in p.meter.values())                 # 콜론으로 쓴 칸 — 칸의 규칙으로는 자동 적재, 검산이 내렸다
        assert (u["reading_kind"], u["hours"]) == ("pending", None)
    assert mixed                                                          # 둘째 날: 시작은 점, 종료는 콜론
    assert paper_run["pipe"].summary["handlers"]["usage"]["format_gate_pending"] >= mixed
    # 그 밖의 쪽: 적힌 대로 읽고(value_raw) 정규화해 적재 — 총이 어긋난 쪽만 빼고 가동 기록이 정답과 같다
    for t in paper_run["truth"]:
        if t["equipment"] == "PUMP" or "TRUCK_total_mismatch" in t["scenarios"]:
            continue
        u, p = rows[t["source"]], plans[t["source"]]
        assert u["review_status"] == "auto" and (u["reading_kind"], u["hours_basis"]) == (t["reading_kind"], t["hours_basis"])
        assert u["meter_start_raw"] == p.meter.get("start")
        for k in ("meter_start", "meter_end", "meter_total", "shift_minutes"):
            assert u[k] == t[k], (t["source"], k)
    # 점으로 쓴 시각은 기계 값의 평가에서 맞다 (same_value) — 사람이 계기 값으로 넣은 8.00 은 최종 값의 평가에서 틀리다
    answers = paper_run["answers"]
    raw = evaluate_fields(con, answers, target="raw", site=paper_run["pipe"].site)
    reading = [g for k, g in raw["by_field_kind"].items() if k.endswith("/reading")]
    assert reading and all(g["cer"] == 0 for g in reading)
    t = pumps[0]
    end = con.execute("SELECT f.*, p.template_name FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                      "WHERE f.field_id = ?", (by_source(con)[t["source"]]["end_field_id"],)).fetchone()
    glyph = plans[t["source"]].meter["end"]
    key = next(k for k in answers if (k[0], k[2], k[3], k[4]) == (t["source"], "meter", end["field_name"], end["row_key"]))
    one = {key: answers[key]}
    save(con, paper_run["pipe"].site, paper_run["settings"], Review(end["field_id"], "value", normalize("reading", glyph), "jp"))
    site = paper_run["pipe"].site
    assert evaluate_fields(con, one, target="raw", only_listed=True, site=site)["field_accuracy"] == 1
    assert evaluate_fields(con, one, target="final", only_listed=True, site=site)["field_accuracy"] == 0
    assert build_report(con)["format_cells"]["reading"]["dotted"] >= len(pumps)


def test_run_answers_wraps_the_oracle_with_the_formats(usage_synth, tmp_path):
    """run --answers 는 [recognize.by_format] 의 형식들로 정답 oracle 을 감싼다 — 받지 않는 형식이면 처리 전에 한 줄로 멈춘다.
    (명령으로 끝까지 돌리는 것은 slow 의 6일치.)"""
    cfg = tmp_path / "bad.toml"
    cfg.write_text(f"[paths]\nreviews = {json.dumps(str(tmp_path / 'r.jsonl'))}\n[recognize.by_format]\ninteger = \"oracle\"\n",
                   encoding="utf-8")
    with pytest.raises(SystemExit, match="인식 백엔드를 준비할 수 없습니다: .*by_kind"):
        main(["run", "--config", str(cfg), "--answers", str(usage_synth.answers_path), "--site", str(usage_synth.site),
              "--archive-root", str(usage_synth.scans), "--work-root", str(tmp_path / "work")])
    assert not (tmp_path / "work" / "minedocscan.db").exists()


# ── generic: V2 의 유류일지·환경일지 ────────────────────────────────────────────
@pytest.mark.slow
def test_v2_forms_read_decimals_and_times_with_the_cell_rule_only(tmp_path):
    syn = generate(tmp_path / "v2", days=1, seed=0, usage_only=True, v2_forms=True)
    answers = load_answers_json(syn.answers_path)
    st = Settings(site=syn.site, archive_root=syn.scans, work_root=tmp_path / "work", reviews=tmp_path / "r.jsonl",
                  save_aligned=False)
    pipe = Pipeline(st, recognizer=by_formats(OracleRecognizer(answers), ALL))
    pipe.run([syn.scans])
    con = pipe.con
    v2 = [dict(r) for r in con.execute(
        "SELECT f.*, p.template_name FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id WHERE p.template_name "
        "IN ('fuel_log', 'env_log') AND f.format IN ('decimal', 'time') AND f.region <> 'fields'")]
    read = [r for r in v2 if r["backend"] == "oracle"]
    assert {r["format"] for r in read} == {"decimal", "time"} and len(read) >= 6
    assert all(r["status_raw"] == "auto" and r["value_final"] == normalize(r["format"], r["value_raw"]) for r in read)
    assert all((r["backend"], r["has_value_raw"], r["value_final"]) == ("ink", 0, "") for r in v2 if r["backend"] != "oracle")
    ev = evaluate_fields(con, answers, target="raw", site=pipe.site)
    groups = {k: g for k, g in ev["by_field_kind"].items() if k.split("/")[0] in ("fuel_log", "env_log")
              and k.rsplit("/", 1)[-1] in ("decimal", "time")}
    assert groups and all(g["cer"] == 0 for g in groups.values())
    assert "generic" not in pipe.summary["handlers"] or not any(
        k.startswith("format_gate_") for k in pipe.summary["handlers"]["generic"])   # 쪽 안 검산은 가동 일보에서만


# ── slow ───────────────────────────────────────────────────────────────────
@pytest.mark.slow
def test_six_days_of_usage_logs_with_the_oracle(tmp_path):
    """수용 기준 그대로: 합성 가동 일보 6일치(인쇄 층 — 인쇄된 ~ 만 있는 칸은 인식기에 가지 않는다) + run --answers + by_format 넷."""
    syn = generate(tmp_path / "synth", days=6, seed=0, usage_only=True, print_layers=True)
    run = run_formats(syn, tmp_path)
    assert sum("TRUCK_total_mismatch" in t["scenarios"] for t in syn.truth["usage"]) == 2
    assert_oracle_reads_format_cells(run)
