"""tasks/0005 단계 2 — 값의 형식: 정규화 표, 템플릿의 format, 검수 거절(파일에 한 줄도 남지 않는다), 인식기에 보내지 않기,
정규화한 표기로 내보내고 비교하기. 시험용 양식은 합성 일보의 야간 칸을 decimal 로 바꾼 것 (기본 합성 데이터는 그대로)."""
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from conftest import clone_db
from minedocscan.config import Settings
from minedocscan.evaluate.fields import evaluate_fields
from minedocscan.forms.formats import (
    FormatError,
    as_number,
    normalize,
    range_minutes,
    reading_kind,
    span_minutes,
)
from minedocscan.forms.template import Template, TemplateError
from minedocscan.pipeline import Pipeline
from minedocscan.recognize import OracleRecognizer, load_answers_json
from minedocscan.recognize.digits.data import read_crops
from minedocscan.review.export import export_crops
from minedocscan.review.server import ApiError, ReviewApp
from minedocscan.review.store import Review, export_answers, load, save
from minedocscan.tools.synth import T_LOG, generate
from test_review_store import TABLES, _dump

ACCEPT = {
    "integer": [("7", "7"), ("07", "7"), ("０７", "7"), (" 12 ", "12")],
    "decimal": [("1234.5", "1234.5"), ("01234.5", "1234.5"), ("1234", "1234"), ("1234.50", "1234.50"), ("0.5", "0.5"),
                ("１２３４．５", "1234.5")],
    "time": [("8:00", "08:00"), ("08:00", "08:00"), ("0800", "08:00"), ("800", "08:00"), ("8", "08:00"), ("08.00", "08:00"),
             ("24:00", "24:00"), ("０８：００", "08:00")],
    "time_range": [("8-17", "08:00~17:00"), ("08:00~17:00", "08:00~17:00"), ("0800-1700", "08:00~17:00"),
                   ("8:30 ~ 17:00", "08:30~17:00"), ("22:00~06:00", "22:00~06:00"), ("8〜17", "08:00~17:00")],
    "reading": [("1234.5", "1234.5"), ("01234.5", "1234.5"), ("08:00", "08:00"), ("8:00", "08:00"),
                ("08.00", "8.00")],          # 콜론이 없으면 계기 값 — 점으로 쓴 시각은 사람이 콜론으로 입력한다
}
REJECT = {
    "integer": ["1.5", "a", "-3", "²", ""],
    "decimal": ["12,5", ".5", "1.2.3", "12a", "1234.", ""],
    "time": ["24:30", "25", "12:60", "8:5", "a", "12:00:00", ""],
    "time_range": ["8", "8-", "8~17~20", "25-26", "8:60-9", ""],
    "reading": ["08:0", "12:60", "abc", "1,5", ""],
}


@pytest.mark.parametrize("fmt", list(ACCEPT))
def test_accepted_inputs_and_their_normal_form(fmt):
    for raw, want in ACCEPT[fmt]:
        assert normalize(fmt, raw) == want, (fmt, raw)
        assert normalize(fmt, want) == want, (fmt, want)                      # 정규화는 두 번 해도 같다 (검수를 다시 덮을 때)


@pytest.mark.parametrize("fmt", list(REJECT))
def test_rejected_inputs(fmt):
    for raw in REJECT[fmt]:
        with pytest.raises(FormatError):
            normalize(fmt, raw)


def test_same_value_different_notation_and_value_helpers():
    assert len({normalize("time_range", t) for t in ("8-17", "08:00~17:00", "0800-1700", "8:00 - 17:00")}) == 1
    assert len({normalize("decimal", t) for t in ("1234.5", "01234.5", "001234.5")}) == 1
    assert normalize(None, "  oil leak ") == "oil leak"                       # 형식이 없으면(글자) 지금과 같다
    assert range_minutes("22:00~06:00") == 480 and range_minutes("08:00~17:00") == 540     # 끝이 이르면 다음 날 (연근)
    assert span_minutes("17:00", "08:00") == 15 * 60 and span_minutes("08:00", "08:00") == 0
    assert (reading_kind("1234.5"), reading_kind("08:00"), reading_kind(None)) == ("meter", "clock", None)
    assert as_number("1234.5") == 1234.5 and as_number("08:00") is None and as_number("x") is None


def _tpl(tmp_path, columns=None, fields=None) -> Path:
    spec = {"name": "t", "reference_image": "ref.png", "handler": "generic",
            "regions": [{"name": "main", "grid": {"ys": [0, 30, 60], "xs": [0, 100, 200]}, "header_rows": 0,
                         "columns": columns or [{"idx": 0, "name": "a", "kind": "handwritten_number"}],
                         "rows": [{"row": 0, "key": "r0"}]}],
            "fields": fields or []}
    p = tmp_path / "template.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return p


def test_template_format_rules(tmp_path):
    t = Template(_tpl(tmp_path, [{"idx": 0, "name": "start", "kind": "handwritten_number", "format": "reading"},
                                 {"idx": 1, "name": "n", "kind": "handwritten_number"}],
                      [{"name": "w", "kind": "handwritten_text", "bbox": [0, 70, 100, 90], "format": "time_range"},
                       {"name": "memo", "kind": "handwritten_text", "bbox": [0, 100, 100, 120]}]))
    fmts = {c.name: c.fmt for c in t.cells() + t.field_cells()}
    assert fmts == {"start": "reading", "n": "integer", "w": "time_range", "memo": None}
    assert (t.format_of("main", "start"), t.format_of("fields", "w"), t.format_of("main", "nope")) == ("reading", "time_range", None)
    for bad, msg in (([{"idx": 0, "name": "p", "kind": "printed", "format": "decimal"}], "손으로 쓰는 칸"),
                     ([{"idx": 0, "name": "c", "kind": "checkmark", "format": "integer"}], "손으로 쓰는 칸"),
                     ([{"idx": 0, "name": "a", "kind": "handwritten_number", "format": "float"}], "알 수 없는 format")):
        with pytest.raises(TemplateError, match=msg):
            Template(_tpl(tmp_path, bad))


# ── 형식이 있는 양식으로 돌린 하루치 ─────────────────────────────────────────
@pytest.fixture(scope="module")
def decimal_day(tmp_path_factory):
    """합성 하루치, 일보의 야간 칸만 decimal. oracle 로 돌린다 — decimal 칸은 인식기에 가지 않아야 한다."""
    root = tmp_path_factory.mktemp("formats")
    syn = generate(root / "data", days=1, seed=0)
    p = syn.site / "templates" / T_LOG / "template.yaml"
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    for reg in spec["regions"]:
        for c in reg["columns"]:
            if c["name"] == "trips_night":
                c["format"] = "decimal"
    p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    settings = Settings(site=syn.site, archive_root=syn.scans, work_root=root / "work", reviews=root / "r" / "reviews.jsonl")
    answers = load_answers_json(syn.answers_path)
    pipe = Pipeline(settings, recognizer=OracleRecognizer(answers))
    pipe.run([syn.scans])
    return {"synth": syn, "settings": settings, "pipe": pipe, "answers": answers, "root": root}


def _cells(con, name):
    return [dict(r) for r in con.execute("SELECT f.*, d.source_name || '#' || p.page_no AS source FROM doc_field f "
                                         "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
                                         "WHERE f.field_name = ? ORDER BY f.field_id", (name,))]


def test_non_integer_cells_are_not_read_and_wait_for_review(decimal_day):
    con = decimal_day["pipe"].con
    night, day = _cells(con, "trips_night"), _cells(con, "trips_day")
    assert {c["format"] for c in night} == {"decimal"} and {c["format"] for c in day} == {"integer"}
    inked = [c for c in night if c["has_value_raw"] == 1]
    assert inked and all(c["value_raw"] is None and c["backend"] == "ink" and c["status_raw"] == "pending" for c in inked)
    assert any(c["backend"] == "oracle" and c["value_raw"] for c in day)       # 정수 칸은 지금처럼 인식기로
    assert {c["format"] for c in _cells(con, "remark")} == {None}             # 글자 칸: 형식 없음


def test_bad_values_never_reach_the_review_file(decimal_day, tmp_path):
    con, site = clone_db(decimal_day["pipe"].con), decimal_day["pipe"].site
    settings = replace(decimal_day["settings"], reviews=tmp_path / "r.jsonl")
    app = ReviewApp(con, site, settings, "jp", "pending")
    night = [c for c in _cells(con, "trips_night") if c["has_value_raw"] == 1]
    a, b = night[0]["field_id"], night[1]["field_id"]
    for bad in ("12,5", "1.2.3", "abc"):
        with pytest.raises(ApiError) as e:
            app.post_review({"field_id": a, "verdict": "value", "value": bad})
        assert e.value.status == 400
        with pytest.raises(FormatError):
            save(con, site, settings, Review(a, "value", bad, "jp"))
    with pytest.raises(ApiError, match="2번째 칸"):                         # 한 칸이라도 틀리면 둘 다 남지 않는다
        app.post_reviews({"items": [{"field_id": a, "verdict": "value", "value": "3.5"},
                                    {"field_id": b, "verdict": "value", "value": "3,5"}]})
    assert not settings.reviews.exists() or load(settings.reviews)[0] == []
    out = app.post_reviews({"items": [{"field_id": a, "verdict": "value", "value": "03.5"},
                                      {"field_id": b, "verdict": "empty", "value": ""}]})
    assert [s["value"] for s in out["saved"]] == ["3.5", ""]
    recs = [rv for _seq, rv in load(settings.reviews)[0]]
    assert [(rv.field_id, rv.value) for rv in recs] == [(a, "3.5"), (b, "")] and recs[0].reviewed_at == recs[1].reviewed_at
    q = app.queue_json({})
    assert q["formats"]["decimal"]["chars"] == "0-9." and "1234.5" in q["formats"]["reading"]["hint"]
    assert {c["format"] for it in q["items"] for c in it["cells"] if c["field_id"] in (a, b)} <= {"decimal"}
    day = _cells(con, "trips_day")[0]["field_id"]                            # 정수 칸은 지금과 같다: "07" → "7"
    assert app.post_review({"field_id": day, "verdict": "value", "value": "07"})["value"] == "7"


def test_answers_eval_and_crops_use_the_normal_form(decimal_day, tmp_path):
    """검수값은 정규화해서 남고, export-answers·eval 은 정규화한 표기로 비교한다. decimal 칸은 숫자 칸 학습 크롭에 들어가지 않는다.
    같은 검수 파일로 새로 돌린 DB 와 같다 (불변식)."""
    con, site, syn = clone_db(decimal_day["pipe"].con), decimal_day["pipe"].site, decimal_day["synth"]
    settings = replace(decimal_day["settings"], reviews=tmp_path / "r.jsonl")
    night = [c for c in _cells(con, "trips_night") if c["has_value_raw"] == 1]
    written = {}
    for k, c in enumerate(night):
        written[c["field_id"]] = f"0{k + 1}.5"                               # 앞의 0 을 붙여 입력 → 1.5, 2.5 …
        save(con, site, settings, Review(c["field_id"], "value", written[c["field_id"]], "jp",
                                         reviewed_at=f"2030-05-01T00:00:{k:02d}Z"))
    assert {r["value_final"] for r in _cells(con, "trips_night") if r["field_id"] in written} == \
           {f"{k + 1}.5" for k in range(len(night))}
    out = tmp_path / "answers.json"
    export_answers(con, out)
    texts = {a["text"] for a in json.loads(out.read_text(encoding="utf-8"))}
    assert {f"{k + 1}.5" for k in range(len(night))} <= texts
    # 정답 파일에 다른 표기로 적혀 있어도 같은 값이면 맞다
    answers = {}
    for c in night:
        src = c["source"]
        answers[(src, T_LOG, c["region"], c["field_name"], c["row_key"])] = "0" + written[c["field_id"]]   # "001.5"
    r = evaluate_fields(con, answers, target="final", only_listed=True)
    g = r["by_field_kind"][f"{T_LOG}/handwritten_number/decimal"]
    assert g["n"] == len(night) and g["field_accuracy"] == 1.0
    crops = export_crops(con, site, settings, tmp_path / "crops", res="aligned")
    lines = [json.loads(x) for lf in (tmp_path / "crops").rglob("labels.jsonl")
             for x in lf.read_text(encoding="utf-8").splitlines()]
    assert crops["written"] == len(lines) and {x["format"] for x in lines} == {"decimal"}
    assert all(x["inked"] for x in lines)                                    # 잉크가 있는 칸 (읽지 않았어도)
    read = read_crops(tmp_path / "crops")                                    # decimal 줄은 숫자 칸 학습에 쓰지 않는다
    assert read.samples == [] and read.skipped["other_format"] == len(lines)
    fresh = Pipeline(replace(settings, work_root=tmp_path / "w2"), recognizer=OracleRecognizer(decimal_day["answers"]))
    fresh.run([syn.scans])
    for t in TABLES:
        assert _dump(fresh.con, t) == _dump(con, t), t
