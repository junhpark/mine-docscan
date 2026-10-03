"""평가와 임계값 (tasks/0003 단계 6) — 크롭 단위 평가(recognizer eval), eval 의 자동 적재 오류율, report 의 백엔드별 수,
회귀 검사의 새 항목. torch 없이, 시험용 모델로. 수치는 합성 셀에 대한 것이다."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import FIXTURE_MODEL, clone_db, digits_metrics, number_cells
from minedocscan.cli import main
from minedocscan.evaluate.fields import evaluate_fields
from minedocscan.evaluate.regression import diff_reports, new_keys
from minedocscan.recognize.digits import calib
from minedocscan.recognize.digits.evaluate import EvalError, evaluate
from minedocscan.report import build_report
from minedocscan.review.export import export_crops
from minedocscan.review.store import Review, import_into
from minedocscan.store.db import open_db, upsert

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def exported(digits_low3, tmp_path_factory):
    """digits 로 돌린 3일치의 복사본에, 인식기가 읽은 숫자 칸 전부를 정답대로 검수해 두고 크롭으로 내보낸 것."""
    root = tmp_path_factory.mktemp("digits_eval")
    pipe = digits_low3["pipe"]
    con = clone_db(pipe.con)
    cells = [c for c in number_cells(con, digits_low3["answers"]) if c["backend"] == "digits"]
    path = root / "reviews.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for c in cells:
            rv = Review(c["field_id"], "value", c["truth"], "jp", reviewed_at="2030-02-01T00:00:00Z") if c["truth"] \
                else Review(c["field_id"], "empty", reviewer="jp", reviewed_at="2030-02-01T00:00:00Z")
            f.write(rv.to_json() + "\n")
    import_into(con, path)
    settings = replace(pipe.settings, reviews=path)
    r = export_crops(con, pipe.site, settings, root / "crops", kind="handwritten_number", res="source")
    return {"root": root, "con": con, "cells": cells, "export": r, "site": pipe.site}


def test_crop_level_eval_equals_pipeline(exported):
    """같은 칸이면 파이프라인의 value_raw 와 export-crops → recognizer eval 이 읽은 값이 같다 (4.1 의 최종 확인)."""
    r = evaluate(exported["root"] / "crops", FIXTURE_MODEL, split=None)
    assert r["cells"] == exported["export"]["written"] == len(exported["cells"]) > 100
    got = {p["field_id"]: (p["text"], p["confidence"]) for p in r["predictions"]}
    for c in exported["cells"]:
        assert got[c["field_id"]][0] == c["value_raw"], c["field_id"]
        assert got[c["field_id"]][1] == pytest.approx(c["confidence"], abs=1e-6)
    # 모델의 기준에서 크롭 단위로 센 자동 적재 = 파이프라인이 자동 적재한 칸
    auto_db = sum(c["status_raw"] == "auto" for c in exported["cells"])
    assert r["at_threshold"]["auto"] == auto_db
    assert r["errors"] == sum(c["value_raw"] != c["truth"] for c in exported["cells"])


def test_recognizer_eval_command(exported, tmp_path, capsys):
    crops = exported["root"] / "crops"
    errs = tmp_path / "errs"
    for split in ("val", "train"):
        assert main(["recognizer", "eval", "--crops", str(crops), "--model", str(FIXTURE_MODEL), "--split", split,
                     "--errors", str(errs), "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert "predictions" not in out and out["split"] == split          # 칸마다의 값은 내놓지 않는다
        assert {"accuracy", "by_value", "confusions", "calibration", "thresholds", "at_threshold", "illegible"} <= set(out)
        assert sum(b["n"] for b in out["calibration"]) == out["cells"] == sum(v["n"] for v in out["by_value"])
    val, train = (evaluate(crops, FIXTURE_MODEL, split=s) for s in ("val", "train"))
    assert val["cells"] + train["cells"] == len([1 for x in (crops / "train" / "labels.jsonl").read_text().splitlines()])
    assert not ({p["field_id"] for p in val["predictions"]} & {p["field_id"] for p in train["predictions"]})
    if out["errors"]:
        assert Path(out["errors_image"]).is_file() and Path(out["errors_image"]).parent == errs
    # test 는 test 로 내보낸 줄만
    has_test = (crops / "test" / "labels.jsonl").exists()
    if has_test:
        assert evaluate(crops, FIXTURE_MODEL, split="test")["cells"] > 0
    # 틀린 칸 모아 보기를 저장소 안에 쓰지 않는다
    with pytest.raises(SystemExit, match="git 작업 트리"):
        main(["recognizer", "eval", "--crops", str(crops), "--model", str(FIXTURE_MODEL), "--errors", str(REPO / "errs")])
    assert not (REPO / "errs").exists()
    # 규격이 모델과 다른 크롭은 거절
    other = tmp_path / "other"
    (other / "train").mkdir(parents=True)
    lines = [json.loads(x) for x in (crops / "train" / "labels.jsonl").read_text().splitlines()]
    (other / "train" / "labels.jsonl").write_text("\n".join(json.dumps(x | {"spec": {"res": "source", "scale": 1.0, "pad": 4},
                                                                         "file": str(crops / x["file"])}) for x in lines))
    with pytest.raises(EvalError, match="규격"):
        evaluate(other, FIXTURE_MODEL, split="train")


def test_eval_reports_auto_accept_error_by_hand():
    """손으로 셀 수 있는 예: 인식기가 자동 적재한 4칸 중 2칸이 틀렸다 (값을 잘못 읽은 것 하나, 값을 빈 칸으로 지운 것 하나)."""
    con = open_db("sqlite:///:memory:")
    upsert(con, "doc_document", {"document_id": "d", "source_path": "x.pdf", "source_name": "x", "status": "processed",
                                 "created_at": "2030-01-01T00:00:00Z"})
    upsert(con, "doc_page", {"page_id": "d-p1", "document_id": "d", "page_no": 1, "status": "loaded",
                             "template_name": "t", "work_date": "2030-01-07"})
    rows = [  # (행, status_raw, backend, has_value_raw, value_raw, 정답)
        (0, "auto", "digits", 1, "3", "3"), (1, "auto", "digits", 1, "4", "9"), (2, "auto", "digits", 0, "", ""),
        (3, "auto", "digits", 0, "", "5"), (4, "pending", "digits", 1, "?", "7"), (5, "auto", "ink", 0, "", "")]
    answers = {}
    for i, st, b, has, raw, truth in rows:
        upsert(con, "doc_field", {"field_id": f"d-p1:r:n:{i}", "page_id": "d-p1", "region": "r", "row_no": i, "field_name": "n",
                                  "kind": "handwritten_number", "row_key": f"k{i}", "has_value_raw": has, "has_value": has,
                                  "value_raw": raw, "value_final": raw, "backend": b, "review_status": st, "status_raw": st})
        if truth:
            answers[("x#1", "t", "r", "n", f"k{i}")] = truth
    res = evaluate_fields(con, answers, target="raw")
    ae = res["auto_error"]
    lo, hi = calib.wilson(2, 4)
    assert (ae["auto"], ae["wrong"], ae["rate"], ae["auto_value"], ae["auto_empty"], ae["ink_auto"]) == (4, 2, 0.5, 2, 2, 1)
    assert ae["ci95"] == [round(lo, 4), round(hi, 4)] == [0.15, 0.85]
    assert res["by_field_kind"]["t/handwritten_number"]["auto_error"] == ae
    # 검수해서 review_status 가 바뀌어도 기계의 판단(status_raw)으로 센다
    con.execute("UPDATE doc_field SET review_status = 'reviewed'")
    assert evaluate_fields(con, answers, target="raw")["auto_error"] == ae


def test_eval_and_report_on_the_digits_run(digits_low3, capsys, tmp_path):
    con = digits_low3["pipe"].con
    res = evaluate_fields(con, digits_low3["answers"], target="raw")
    m = digits_metrics(number_cells(con, digits_low3["answers"]))
    assert res["auto_error"]["auto"] == m["auto"] and res["auto_error"]["wrong"] == m["auto_wrong"]
    assert res["auto_error"]["rate"] <= 0.01
    rep = build_report(con)
    by = rep["fields_by_backend"]
    n_digits = sum(c["backend"] == "digits" for c in number_cells(con, digits_low3["answers"]))
    assert by["digits"]["auto"] >= m["auto"] and by["digits"]["fields"] >= n_digits    # 운반 표 밖의 숫자 칸도 있다
    assert by["digits"]["pending"] == by["digits"]["fields"] - by["digits"]["auto"]
    assert set(by) >= {"digits", "ink", "null"}
    p = digits_low3["pipe"]
    common = ["--site", str(p.settings.site), "--work-root", str(p.settings.work_root)]
    answers = digits_low3["synth"].answers_path
    assert main(["eval", "--answers", str(answers), "--target", "raw", *common]) == 0
    out = capsys.readouterr().out
    assert "자동 적재 오류율" in out and f"{m['auto_wrong']}/{m['auto']}" in out
    assert main(["report", *common]) == 0 and "수기 칸 백엔드별: " in capsys.readouterr().out


def test_regress_ignores_new_report_groups_but_not_changes():
    base = {"documents": 3, "documents_by_status": {"processed": 3}, "fields": {"pending": 5}}
    now = base | {"warnings": {"n": 0, "documents": []}, "fields_by_backend": {"null": {"fields": 9}}}
    assert diff_reports(base, now) == [] and new_keys(base, now) == ["fields_by_backend.null.fields", "warnings.documents",
                                                                      "warnings.n"]
    worse = now | {"documents_by_status": {"processed": 2, "failed": 1}, "fields": {"pending": 6}}
    assert diff_reports(base, worse) == [("documents_by_status.failed", None, 1), ("documents_by_status.processed", 3, 2),
                                         ("fields.pending", 5, 6)]
