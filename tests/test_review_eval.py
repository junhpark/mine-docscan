"""검수값으로 평가하기: export-answers → eval --target raw, 값 유무 정밀도·재현율, 교차검증 일치율."""
import json
from dataclasses import replace

import pytest

from minedocscan.cli import main
from minedocscan.evaluate.fields import evaluate_fields, evaluate_presence
from minedocscan.pipeline import Pipeline
from minedocscan.recognize import OracleRecognizer, load_answers_json
from minedocscan.report import build_report, format_report
from minedocscan.review.store import stats
from minedocscan.tools.synth import T_INSP, T_LOG, T_MATRIX, UG_ROWS


@pytest.fixture(scope="module")
def reviewed(reviewed_day):
    return reviewed_day


def test_exported_answers_match_truth(reviewed):
    exported = load_answers_json(reviewed["exported"])
    truth = reviewed["answers"]
    # 검수에서 나온 정답(빈 칸은 "")에서 값이 있는 것만 보면 합성 정답과 같다. 점검내역은 날짜 키였던 것이 쪽 키가 된다
    got = {k[1:]: v for k, v in exported.items() if v}
    want = {k[1:]: v for k, v in truth.items() if v}
    assert got == want
    assert all(v == "" or v for v in exported.values()) and len(exported) > len(want)      # 빈 칸도 명시된다


def test_null_backend_scores_zero_on_values_and_one_on_empties(reviewed):
    con = reviewed["null"].con
    ans = load_answers_json(reviewed["exported"])
    final = evaluate_fields(con, ans, target="final")
    assert final["field_accuracy"] == 1.0                                   # final 은 검수값 자신이라 언제나 맞는다
    raw = evaluate_fields(con, ans, target="raw", only_listed=True)
    assert raw["accuracy_value"] == 0.0 and raw["accuracy_empty"] == 1.0 and raw["answers_not_in_db"] == 0
    assert raw["n"] == raw["n_value"] + raw["n_empty"] == len(ans)
    for k, g in raw["by_field_kind"].items():
        assert g["accuracy_value"] == 0.0 and g["accuracy_empty"] == 1.0, k
    pr = evaluate_presence(con)
    assert pr["precision"] == 1.0 and pr["recall"] == 1.0 and pr["fp"] == pr["fn"] == 0 and pr["n"] == len(ans)
    rep = build_report(con)
    assert rep["xcheck_agreement"]["raw"] == {"both": 0, "same": 0, "rate": None}        # 기계 값이 없다
    assert rep["xcheck_agreement"]["final"]["rate"] is not None                           # 최종 값(검수값)으로는 잴 수 있다
    assert "횟수 일치율" in format_report(rep)


def test_oracle_backend_scores_one_against_reviews(reviewed):
    s = replace(reviewed["settings"], work_root=reviewed["root"] / "work_oracle")
    pipe = Pipeline(s, recognizer=OracleRecognizer(reviewed["answers"]))
    pipe.run([reviewed["synth"].scans])
    ans = load_answers_json(reviewed["exported"])
    raw = evaluate_fields(pipe.con, ans, target="raw", only_listed=True)
    assert raw["field_accuracy"] == 1.0 and raw["cer"] == 0.0 and raw["accuracy_value"] == 1.0
    pr = evaluate_presence(pipe.con)
    assert pr["precision"] == 1.0 and pr["recall"] == 1.0
    # 기계 값 기준 일치율 = (양쪽 값 있는 칸 − 횟수가 다른 칸) / 양쪽 값 있는 칸 — 정답에서 독립적으로 계산
    day = reviewed["synth"].truth["days"][0]
    matrix = {(r["slot"], r["material"], r["level"]): r["trips"] for r in day["haul_matrix"]}
    log: dict = {}
    for r in day["haul_log"]:
        k = (r["slot"], r["material"], r["level"])
        log[k] = log.get(k, 0) + r["trips"]
    both = [k for k in log if k in matrix and k[1:] in UG_ROWS]
    same = sum(log[k] == matrix[k] for k in both)
    rep = build_report(pipe.con)
    assert rep["xcheck_agreement"]["raw"] == {"both": len(both), "same": same, "rate": round(same / len(both), 4)}
    assert rep["xcheck_agreement"]["final"] == rep["xcheck_agreement"]["raw"]            # 검수값 = 정답 = 오라클
    assert same < len(both)                                                                # 횟수가 다른 칸이 하나 있다


def test_stats_and_cli_commands(reviewed, capsys):
    con, settings, synth = reviewed["null"].con, reviewed["settings"], reviewed["synth"]
    st = stats(con)
    assert st["records"] >= st["fields"] and set(st["by_verdict"]) == {"value", "empty"}
    assert set(st["by_template"]) == {T_INSP, T_LOG, T_MATRIX} and st["by_reviewer"] == {"jp": st["records"]}
    assert st["bbox_changed"] == 0 and st["fields_not_in_db"] == 0 and list(st["by_date"]) == [synth.truth["days"][0]["date"]]

    common = ["--site", str(synth.site), "--work-root", str(settings.work_root), "--db-url", settings.resolved_db_url]
    env = {"MINEDOCSCAN_REVIEWS": str(settings.reviews)}
    import os
    old = os.environ.get("MINEDOCSCAN_REVIEWS")
    os.environ.update(env)
    try:
        assert main(["review", "stats", "--json"] + common) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["stats"]["records"] == st["records"]
        out_path = reviewed["root"] / "cli_answers.json"
        assert main(["review", "export-answers", str(out_path)] + common) == 0
        assert "--only-listed" in capsys.readouterr().out
        assert main(["eval", "--answers", str(out_path), "--target", "raw", "--only-listed", "--json"] + common) == 0
        ev = json.loads(capsys.readouterr().out)
        assert ev["fields"]["target"] == "raw" and ev["fields"]["accuracy_value"] == 0.0 and ev["presence"]["recall"] == 1.0
    finally:
        if old is None:
            os.environ.pop("MINEDOCSCAN_REVIEWS", None)
        else:
            os.environ["MINEDOCSCAN_REVIEWS"] = old


def test_serve_requires_reviewer(reviewed):
    synth, settings = reviewed["synth"], reviewed["settings"]
    with pytest.raises(SystemExit, match="--reviewer"):
        main(["review", "serve", "--site", str(synth.site), "--work-root", str(settings.work_root)])
