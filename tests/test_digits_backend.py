"""숫자 인식 백엔드 digits 와 자동 적재 규칙 (tasks/0003 단계 5) — torch 없이, 시험용 모델(tests/fixtures/digits-fixture)로.

시험용 모델은 합성 셀만으로 학습한 것이다. 여기의 문턱은 합성 셀에 대한 것이고 손글씨 인식률이 아니다.
"""
import json
import shutil
from dataclasses import replace

import numpy as np
import pytest

from conftest import (
    FIXTURE_MODEL,
    answers_of,
    clone_db,
    day_pdf,
    digits_metrics,
    digits_settings,
    number_cells,
)
from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.evaluate.fields import evaluate_presence
from minedocscan.handlers.base import number_status
from minedocscan.imaging.cropspec import CropSpec
from minedocscan.pipeline import Pipeline
from minedocscan.recognize import CellContext, Recognition, build_recognizer
from minedocscan.recognize.digits.backend import DigitsRecognizer
from minedocscan.report import build_report
from minedocscan.review.store import Review, import_into, save
from minedocscan.tools.synth_cells import CellParams, make_cells
from test_review_store import TABLES, _dump


def test_fixture_model_reads_synthetic_forms(digits_low3):
    """합성 3일치: 값 있는 숫자 칸의 정확도 ≥ 0.95, 자동 적재된 칸 중 틀린 것 ≤ 1 %, 횟수 일치율(기계 값)이 0/0 이 아니다."""
    con = digits_low3["pipe"].con
    m = digits_metrics(number_cells(con, digits_low3["answers"]))
    print("합성 3일치:", json.dumps(m))
    assert m["values"] > 100 and m["auto"] > 100
    assert m["value_correct"] / m["values"] >= 0.95
    assert m["auto_wrong"] / m["auto"] <= 0.01
    agree = build_report(con)["xcheck_agreement"]["raw"]
    print("횟수 일치율(기계 값):", agree)
    assert agree["both"] > 0 and agree["same"] > 0
    # 범위(trips_max = 40) 안의 값만 자동 적재된다
    assert all(int(c["value_raw"]) <= 40 for c in number_cells(con, digits_low3["answers"])
               if c["review_status"] == "auto" and c["has_value_raw"] and c["backend"] == "digits")


def test_x_marks_are_auto_loaded_as_empty_and_presence_improves(digits_low3, tmp_path):
    """X 표·이웃 칸 글씨: 잉크로는 값 있음이지만 값 없음으로 자동 적재된다. 그 칸들을 정답대로 검수해 두고 재면
    evaluate_presence 의 정밀도는 null 보다 높고 재현율은 떨어지지 않는다 (첫째 날의 숫자 칸)."""
    synth, answers = digits_low3["synth"], digits_low3["answers"]
    m = digits_metrics(number_cells(digits_low3["pipe"].con, answers))
    assert m["inked_empty"] > 0 and m["inked_empty_auto"] / m["inked_empty"] >= 0.5
    pdf = day_pdf(synth, 0)
    null = Pipeline(Settings(site=synth.site, archive_root=synth.scans, work_root=tmp_path / "null", reviews=tmp_path / "n.jsonl",
                             save_aligned=False))
    null.run([pdf])
    day_answers = answers_of(answers, [pdf], synth)
    res = {}
    for name, con in (("null", null.con), ("digits", clone_db(digits_low3["pipe"].con))):
        path = tmp_path / f"{name}.jsonl"
        with open(path, "w", encoding="utf-8") as f:                 # 정답대로: 값이 있으면 value, 없으면 empty
            for c in number_cells(con, answers):
                if not c["source"].startswith(pdf.stem):
                    continue
                at = "2030-02-01T00:00:00Z"
                rv = Review(c["field_id"], "value", c["truth"], "jp", reviewed_at=at) if c["truth"] \
                    else Review(c["field_id"], "empty", reviewer="jp", reviewed_at=at)
                f.write(rv.to_json() + "\n")
        import_into(con, path)
        res[name] = evaluate_presence(con)
    print("값 유무:", json.dumps(res))
    assert res["digits"]["n"] == res["null"]["n"] > 50 and day_answers
    assert res["null"]["fp"] > 0                                      # null 은 X 표·넘어온 글씨를 값으로 본다
    assert res["digits"]["precision"] > res["null"]["precision"]
    assert res["digits"]["recall"] >= res["null"]["recall"]


def test_number_status_table():
    """4.4 의 표 (기준 t = 0.9)."""
    s = Settings(auto_accept_conf=0.5)

    def st(text, conf, answer, max_value=None):
        return number_status(Recognition(text, conf, [], "digits", answer=answer, threshold=0.9), s, max_value)

    assert st("7", 0.95, "value") == (True, "auto") and st("7", 0.8, "value") == (True, "pending")
    assert st("45", 0.99, "value", 40) == (True, "pending")                # 범위 밖: 신뢰도가 높아도 검수
    assert st("40", 0.99, "value", 40) == (True, "auto")
    assert st("", 0.95, "empty") == (False, "auto") and st("", 0.8, "empty") == (True, "pending")
    assert st("?", 0.99, "reject") == (True, "pending")
    # 기준이 없는 백엔드는 [pipeline] auto_accept_conf, 기준이 inf(자동 적재 없음)이면 늘 대기
    assert number_status(Recognition("7", 0.6, [], "x", answer="value"), s) == (True, "auto")
    assert number_status(Recognition("7", 1.0, [], "x", answer="value", threshold=float("inf")), s)[1] == "pending"


def test_backend_declares_the_card_spec_and_skips_other_kinds():
    rec = DigitsRecognizer(FIXTURE_MODEL)
    assert rec.crop_spec_for("handwritten_number") == CropSpec.from_dict(rec.card["spec"]) == CropSpec("source", 1.5, None)
    assert rec.crop_spec_for("handwritten_text") is None
    cells = make_cells(4, seed=3, params=CellParams(cell_w=112, cell_h=22))
    ctx = [CellContext("t", "r", "f", "handwritten_number", "k") for _ in cells[:3]] + \
          [CellContext("t", "r", "f", "handwritten_text", "k")]
    out = rec.recognize([c.image for c in cells], ctx)
    for r in out[:3]:
        assert r.backend == "digits" and r.answer in ("value", "empty", "reject") and 0 <= r.confidence <= 1
        assert r.candidates[0] == r.text and len(r.candidates) <= 5 and r.threshold == rec.threshold
    assert (out[3].text, out[3].confidence, out[3].answer) == ("", 0.0, None)          # 읽지 않는다 → 검수 대기


def test_threshold_priority_and_missing_model(tmp_path, low_synth):
    card_t = DigitsRecognizer(FIXTURE_MODEL).threshold
    s = digits_settings(low_synth, tmp_path / "w")
    assert build_recognizer(s).backend_for("handwritten_number").threshold == card_t           # 카드의 기준
    s2 = replace(s, recognizer_options={"digits": {"model": str(FIXTURE_MODEL), "auto_accept_conf": 0.99}})
    assert build_recognizer(s2).backend_for("handwritten_number").threshold == 0.99            # 설정이 이긴다
    # 모델을 이름으로: 사이트 팩의 models/<이름>
    site = tmp_path / "site"
    shutil.copytree(low_synth.site, site)
    shutil.copytree(FIXTURE_MODEL, site / "models" / "fx")
    s3 = replace(s, site=site, recognizer_options={"digits": {"model": "fx"}})
    assert build_recognizer(s3).backend_for("handwritten_number").model_dir == site / "models" / "fx"
    # 이름은 현재 폴더에 같은 이름의 폴더가 있어도 사이트 팩의 것이다
    import os

    cwd = os.getcwd()
    try:
        (tmp_path / "cwd" / "fx").mkdir(parents=True)
        os.chdir(tmp_path / "cwd")
        assert build_recognizer(s3).backend_for("handwritten_number").model_dir == site / "models" / "fx"
    finally:
        os.chdir(cwd)
    # 모델이 없으면 시작할 때 무엇이 없는지 말하고 멈춘다 (null 로 물러나지 않는다)
    cfg = tmp_path / "minedocscan.toml"
    cfg.write_text('[recognize.by_kind]\nhandwritten_number = "digits"\n[recognize.digits]\nmodel = "nope"\n', encoding="utf-8")
    common = ["--config", str(cfg), "--site", str(site), "--archive-root", str(low_synth.scans)]
    db = tmp_path / "w2" / "minedocscan.db"
    db.parent.mkdir()
    db.write_bytes(b"")                                                    # 있던 DB 는 모델이 없으면 지우지 않는다 (--fresh 라도)
    with pytest.raises(SystemExit, match=r"model\.onnx.*nope|nope.*model\.onnx"):
        main(["run", "--fresh", *common, "--work-root", str(tmp_path / "w2")])
    assert db.exists()
    cfg.write_text('[recognize.by_kind]\nhandwritten_number = "digits"\n', encoding="utf-8")
    with pytest.raises(SystemExit, match=r"\[recognize\.digits\] model"):
        main(["run", *common, "--work-root", str(tmp_path / "w3")])
    # 카드와 다른 model.onnx 는 받지 않는다
    bad = tmp_path / "bad"
    shutil.copytree(FIXTURE_MODEL, bad)
    with open(bad / "model.onnx", "ab") as f:
        f.write(b"\0")
    with pytest.raises(ValueError, match="SHA-256"):
        DigitsRecognizer(bad)


def test_info_shows_backend_and_model(low_synth, tmp_path, capsys):
    cfg = tmp_path / "info.toml"
    cfg.write_text('[recognize.by_kind]\nhandwritten_number = "digits"\n[recognize.digits]\n'
                   f'model = "{FIXTURE_MODEL.as_posix()}"\n', encoding="utf-8")
    assert main(["info", "--config", str(cfg), "--site", str(low_synth.site), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    d = info["recognizer"]["by_kind"]["handwritten_number"]
    assert d["backend"] == "digits" and d["model"] == "digits-fixture" and d["spec"].startswith("source")
    assert d["synthetic_cells"] > 0 and d["train_cells"] == 0
    assert info["recognizer"]["by_kind"]["handwritten_text"]["backend"] == "null"
    assert "digits" in info["backends"]["recognizers"]
    # 카드의 기준이면 그 기준의 검증 오류율 95 % 상한을 같이 보인다 (기준만 보고 믿지 않도록)
    card = json.loads((FIXTURE_MODEL / "card.json").read_text(encoding="utf-8"))["auto_accept"]
    assert d["auto_accept_source"] == "card" and d["auto_accept_upper95"] == card["upper95"] < 0.02
    assert card["auto"] >= card["min_auto"] == 100
    assert main(["info", "--config", str(cfg), "--site", str(low_synth.site)]) == 0
    assert f"95 % 상한 {card['upper95']:.1%}" in capsys.readouterr().out
    # 설정으로 준 기준에는 검증 근거가 없다 — 상한을 보이지 않는다
    cfg.write_text(cfg.read_text(encoding="utf-8") + "auto_accept_conf = 0.9\n", encoding="utf-8")
    assert main(["info", "--config", str(cfg), "--site", str(low_synth.site), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)["recognizer"]["by_kind"]["handwritten_number"]
    assert d["auto_accept_conf"] == 0.9 and d["auto_accept_source"] == "config" and d["auto_accept_upper95"] is None


def test_invariant_holds_with_digits_and_range_is_checked(low_synth, tmp_path):
    """digits 로 돌린 DB 에 검수를 저장한 직후 == 같은 검수 파일로 새로 돌린 DB (둘째 날). 그리고 범위 밖은 검수 대기."""
    site = tmp_path / "site"
    shutil.copytree(low_synth.site, site)
    toml = site / "site.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace("trips_max = 40", "trips_max = 4"), encoding="utf-8")
    pdf = day_pdf(low_synth, 1)
    settings = replace(digits_settings(low_synth, tmp_path / "work", reviews=tmp_path / "r.jsonl"), site=site)
    live = Pipeline(settings)
    live.run([pdf])
    con = live.con
    from minedocscan.recognize import load_answers_json

    cells = number_cells(con, load_answers_json(low_synth.answers_path))
    over = [c for c in cells if c["backend"] == "digits" and c["value_raw"].isdigit() and int(c["value_raw"]) > 4]
    assert over and all(c["review_status"] == "pending" for c in over)
    # 범위는 운반 횟수 칸만: 같은 쪽 곁표의 숫자 칸은 4 보다 커도 신뢰도가 높으면 자동 적재된다
    side = con.execute("SELECT value_raw, review_status, confidence FROM doc_field WHERE kind = 'handwritten_number' "
                       "AND backend = 'digits' AND region NOT IN ('haul', 'matrix')").fetchall()
    t = build_recognizer(settings).backend_for("handwritten_number").threshold
    sure_big = [r for r in side if r[0].isdigit() and int(r[0]) > 4 and r[2] >= t]
    assert all(r[1] == "auto" for r in sure_big)
    # 아무 검수나: 값·빈 칸·읽을 수 없음을 섞어서, 자동 적재된 빈 칸을 값으로 바꿨다가 읽을 수 없음으로
    rng = np.random.default_rng(0)
    picks = [cells[i] for i in rng.choice(len(cells), size=min(24, len(cells)), replace=False)]
    for k, c in enumerate(picks):
        verdict = ("value", "empty", "illegible")[k % 3]
        save(con, live.site, settings, Review(c["field_id"], verdict, "3" if verdict == "value" else "", "jp",
                                              reviewed_at=f"2030-02-01T00:00:{k:02d}Z"))
    auto_empty = [c for c in cells if c["has_value_raw"] == 0 and c["backend"] == "digits"]
    assert auto_empty
    save(con, live.site, settings, Review(auto_empty[0]["field_id"], "value", "9", "jp", reviewed_at="2030-02-01T00:01:00Z"))
    save(con, live.site, settings, Review(auto_empty[0]["field_id"], "illegible", reviewer="jp",
                                          reviewed_at="2030-02-01T00:01:01Z"))
    fresh = Pipeline(replace(settings, work_root=tmp_path / "work2"))
    fresh.run([pdf])
    assert build_report(fresh.con) == build_report(con)
    for t in TABLES:
        assert _dump(fresh.con, t) == _dump(con, t), t
