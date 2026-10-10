"""파이프라인에 메타 필드 읽기 (tasks/0004 단계 5): 채우기, 대조, 평가, 대기열 — torch 없이 시험용 모델로.

수치는 합성 메타 필드에 대한 문턱이다 — 인식률이 아니다.
"""
import json
from dataclasses import replace

import numpy as np

from conftest import clone_db, meta_run
from minedocscan.cli import main
from minedocscan.evaluate.meta import evaluate_meta
from minedocscan.pipeline import Pipeline
from minedocscan.report import build_report, meta_mismatch_pages
from minedocscan.review.queue import build_queue
from minedocscan.review.server import ReviewApp
from minedocscan.review.store import Review, field_id_of, save
from minedocscan.tools import synth_meta
from minedocscan.tools.synth import T_LOG, generate
from test_review_store import TABLES, _dump


def _log_pages(con) -> dict:
    return {f"{r['source_name']}#{r['page_no']}": r["page_id"] for r in con.execute(
        "SELECT p.page_id, p.page_no, d.source_name FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
        "WHERE p.template_name = ? ORDER BY 1", (T_LOG,))}


def _meta(con) -> dict:
    return {(r["page_id"], r["meta_key"]): dict(r) for r in con.execute("SELECT * FROM doc_page_meta")}


def _slots(con) -> dict:
    return {r[0]: r[1] for r in con.execute("SELECT page_id, MAX(slot) FROM prod_haul WHERE source_role='log' GROUP BY 1")}


def test_machine_fills_meta_when_labels_are_missing(meta_truth, meta_nolabels, meta_null):
    """라벨이 없으면 차량번호·작성자를 기계 값으로 채운다: 자리가 맞게 정해진 쪽 0.95 이상, 자동 적재된 메타 중 틀린 것 2 % 이하.
    처음 보는 차는 목록에 없는 값, 처음 보는 사람은 기준 미만 — 채우지 않는다 (차량번호로 자리를 찾는다)."""
    con, lab = meta_nolabels["pipe"].con, meta_null["pipe"].con          # 라벨이 있는 실행 (라벨이 이기므로 모델과 무관)
    pages = _log_pages(con)
    meta = _meta(con)
    auto = [(src, k) for src, pid in pages.items() for k in ("vehicle_no", "operator")
            if meta[(pid, k)]["machine_status"] == "auto"]
    wrong = [(s, k) for s, k in auto if meta[(pages[s], k)]["machine_value"] != meta_truth[s][k]]
    assert len(auto) >= 0.85 * 2 * len(pages) and len(wrong) <= 0.02 * len(auto)
    for src, pid in pages.items():
        for k in ("vehicle_no", "operator"):
            row = meta[(pid, k)]
            assert row["source"] == ("machine" if row["machine_status"] == "auto" else None) and row["check_result"] == "none"
            if row["machine_status"] == "auto":
                assert row["value"] == row["machine_value"]
        if meta_truth[src]["vehicle_no"] in synth_meta.NEW_VEHICLES:
            assert meta[(pid, "vehicle_no")]["machine_status"] == "unlisted" and meta[(pid, "vehicle_no")]["value"] is None
        if meta_truth[src]["operator"] in synth_meta.STRANGERS:
            assert meta[(pid, "operator")]["machine_status"] != "auto"
    slots, want = _slots(con), _slots(lab)
    right = sum(slots[p] == want[p] and want[p] is not None for p in pages.values())
    assert right >= 0.95 * len(pages)
    # 비교 결과(운반 수·판정)는 라벨을 준 실행과 같다. 관측한 차·사람은 기계가 채운 것만 — 채운 것은 라벨과 같다
    no_obs = lambda c: [r[:4] + r[6:] for r in _dump(c, "xcheck_haul")]     # noqa: E731 — operator·vehicle_no 빼고
    assert no_obs(con) == no_obs(lab)
    for r, w in zip(_dump(con, "xcheck_haul"), _dump(lab, "xcheck_haul"), strict=True):
        assert all(a is None or a == b for a, b in zip(r[4:6], w[4:6], strict=True))
    obs, obs_lab = _dump(con, "eq_assignment_obs"), _dump(lab, "eq_assignment_obs")
    assert [r[:2] for r in obs] == [r[:2] for r in obs_lab]
    for r, w in zip(obs, obs_lab, strict=True):
        assert all(a is None or a == b for a, b in zip(r[2:6], w[2:6], strict=True))
    rep = build_report(con)
    assert rep["log_slots"]["resolved"] == len(pages) and set(rep["log_slots"]["by_source"]) <= {"machine"}
    assert rep["page_meta"]["vehicle_no"]["by_source"].get("machine") == len(auto) - sum(k == "operator" for _s, k in auto)
    # 기계 값의 doc_field 행: 백엔드, 상태, 후보
    f = con.execute("SELECT * FROM doc_field WHERE field_id = ?", (field_id_of(next(iter(pages.values())), "operator"),)).fetchone()
    assert f["backend"] == "meta-choice" and f["status_raw"] in ("auto", "pending") and json.loads(f["candidates"])


def test_labels_win_and_a_wrong_label_shows_as_mismatch(meta_mislabeled, meta_truth, tmp_path):
    """라벨이 있으면 최종 값은 라벨이고 기계 값은 대조만. 일부러 틀리게 적은 라벨은 mismatch — 최종 값은 바뀌지 않는다.
    meta-check: 두 값을 보여 주고, 기계 값을 입력하거나(match 가 된다) 읽을 수 없음으로 답해도 끝난 것으로 센다 (분모가 줄지 않는다)."""
    run = meta_mislabeled
    con = clone_db(run["pipe"].con)
    site = run["pipe"].site
    pages = _log_pages(con)
    wrong = {pages[src]: src for src in run["wrong"]}
    meta = _meta(con)
    for pid, src in wrong.items():
        row = meta[(pid, "vehicle_no")]
        assert (row["value"], row["source"], row["check_result"]) == ("4999", "label", "mismatch")
        assert row["machine_value"] == meta_truth[src]["vehicle_no"]
        assert {r[0] for r in con.execute("SELECT DISTINCT vehicle_no FROM prod_haul WHERE page_id=?", (pid,))} == {"4999"}
    mm = meta_mismatch_pages(con)
    assert sorted((m["page_id"], m["meta_key"]) for m in mm) == sorted((p, "vehicle_no") for p in wrong)
    assert {m["value_source"] for m in mm} == {"label"}
    checks = {r["check_result"] for (p, k), r in meta.items() if k in ("vehicle_no", "operator") and p not in wrong}
    assert checks <= {"match", "unread"} and "match" in checks
    # meta-check 대기열
    q = build_queue(con, "meta-check", site=site)
    assert (q["total"], q["done"], len(q["items"])) == (2, 0, 2)
    cells = [it["cells"][0] for it in q["items"]]
    for c in cells:
        src = wrong[c["field_id"].split(":fields:")[0]]
        assert c["human"] == {"value": "4999", "source": "label"} and c["machine"]["value_raw"] == meta_truth[src]["vehicle_no"]
    settings = replace(run["settings"], reviews=tmp_path / "r.jsonl")
    app = ReviewApp(con, site, settings, "jp", "meta-check")
    app.post_review({"field_id": cells[0]["field_id"], "verdict": "value", "value": cells[0]["machine"]["value_raw"]})
    pid0 = cells[0]["field_id"].split(":fields:")[0]
    row = _meta(con)[(pid0, "vehicle_no")]
    assert (row["source"], row["check_result"]) == ("review", "match")
    app.post_review({"field_id": cells[1]["field_id"], "verdict": "illegible", "value": ""})
    q2 = build_queue(con, "meta-check", site=site)
    assert q2["items"] == [] and (q2["total"], q2["done"]) == (2, 2)
    # 명령줄: 값은 찍지 않는다
    out = run["settings"]
    assert main(["pages", "--meta-mismatch", "--site", str(out.site), "--work-root", str(out.work_root)]) == 0


def test_a_page_from_another_day_shows_as_date_mismatch(tmp_path):
    """다른 날의 쪽이 섞인 묶음: 그 쪽의 읽은 월·일이 파일의 날짜와 달라 date mismatch. work_date 는 바뀌지 않는다."""
    syn = generate(tmp_path / "data", days=2, seed=5, meta_fields=True, mix_pages=True)
    run = meta_run(syn, tmp_path / "run", inputs=[sorted(syn.scans.glob("*.pdf"))[-1]])     # 섞인 쪽이 있는 마지막 날만
    con = run["pipe"].con
    mixed = [(stem, p) for stem, info in syn.truth["documents"].items() for p in info if p.get("mixed_from")]
    assert len(mixed) == 1
    stem, info = mixed[0]
    pid = con.execute("SELECT p.page_id FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
                      "WHERE d.source_name = ? AND p.page_no = ?", (stem, info["page"])).fetchone()[0]
    meta = _meta(con)
    assert meta[(pid, "date.day")]["check_result"] == "mismatch" and meta[(pid, "date")]["check_result"] == "mismatch"
    assert con.execute("SELECT work_date FROM doc_page WHERE page_id=?", (pid,)).fetchone()[0] == syn.truth["days"][-1]["date"]
    others = [r["check_result"] for (p, k), r in meta.items() if k == "date.day" and p != pid]
    assert others and set(others) == {"match"}
    rows = meta_mismatch_pages(con, "date.day")
    assert [r["page_id"] for r in rows] == [pid]


def test_invariant_with_meta_models(meta_synth, meta_mislabeled, tmp_path):
    """메타 모델을 켠 DB 에 아무 검수(메타 필드 포함)를 저장한 직후 == 같은 검수 파일로 새로 돌린 DB. 기계 열은 검수 전후가 같다.
    meta_mislabeled(첫날, 라벨 있음 — 검수가 라벨을 이긴다)를 복사해서."""
    day = sorted(meta_synth.scans.glob("*.pdf"))[0]
    base = meta_mislabeled["pipe"]
    con = clone_db(base.con)
    settings = replace(meta_mislabeled["settings"], reviews=tmp_path / "r.jsonl")
    machine_cols = ("value_raw", "has_value_raw", "confidence", "backend", "status_raw", "candidates")
    before_f = {r["field_id"]: tuple(r[c] for c in machine_cols) for r in con.execute("SELECT * FROM doc_field")}
    before_m = {(r["page_id"], r["meta_key"]): (r["machine_value"], r["machine_confidence"], r["machine_status"])
                for r in con.execute("SELECT * FROM doc_page_meta")}
    pages = list(_log_pages(con).values())
    rng = np.random.default_rng(0)
    plan = []
    for k, pid in enumerate(rng.choice(pages, size=min(4, len(pages)), replace=False)):
        key = ("vehicle_no", "operator")[k % 2]
        verdict = ("value", "empty", "illegible")[k % 3]
        plan.append(Review(field_id_of(str(pid), key), verdict, "4183" if (verdict == "value" and key == "vehicle_no")
                           else ("BRAVO" if verdict == "value" else ""), "jp", reviewed_at=f"2030-04-01T00:00:{k:02d}Z"))
    haul = con.execute("SELECT field_id FROM doc_field WHERE region = 'haul' AND has_value = 1 LIMIT 1").fetchone()[0]
    plan.append(Review(haul, "value", "7", "jp", reviewed_at="2030-04-01T00:01:00Z"))
    plan.append(Review(field_id_of(pages[0], "date_day"), "value", "30", "jp", reviewed_at="2030-04-01T00:01:01Z"))
    for rv in plan:
        save(con, base.site, settings, rv)
    fresh = Pipeline(replace(settings, work_root=tmp_path / "w_fresh"))
    fresh.run([day])
    assert build_report(fresh.con) == build_report(con)
    for t in TABLES:
        assert _dump(fresh.con, t) == _dump(con, t), t
    after_f = {r["field_id"]: tuple(r[c] for c in machine_cols) for r in con.execute("SELECT * FROM doc_field")}
    after_m = {(r["page_id"], r["meta_key"]): (r["machine_value"], r["machine_confidence"], r["machine_status"])
               for r in con.execute("SELECT * FROM doc_page_meta")}
    assert after_f == before_f and after_m == before_m
    # 검수에서 빈 칸이라고 답한 키는 비어 있다 — 기계 값이 채우지 않는다 (사람의 답이 이긴다)
    for rv in plan:
        if rv.verdict == "empty":
            pid, key = rv.field_id.split(":fields:")[0], rv.field_id.split(":")[-2]
            row = con.execute("SELECT * FROM doc_page_meta WHERE page_id = ? AND meta_key = ?", (pid, key)).fetchone()
            assert (row["value"], row["source"]) == (None, "review"), key
            items = build_queue(con, "page-fields", site=base.site)["items"]
            assert not any(c["field_id"] == rv.field_id for it in items for c in it["cells"])     # 다시 묻지 않는다


def test_label_date_that_is_not_iso_and_regress_without_meta_models(meta_synth, meta_null, meta_mislabeled):
    """ISO 가 아닌 라벨 날짜("2030.01.07")는 쪽을 오류로 만들지 않는다 — 월·일 대조만 하지 않는다.
    regress 는 설정의 [recognize.meta] 를 쓰지 않는다 (null 기준이 메타 모델에 흔들리지 않게)."""
    from minedocscan import pagemeta
    from minedocscan.evaluate.regression import run_regression

    class Site:
        labels = {"doc": {"date": "2030.01.07"}}

        @staticmethod
        def date_from_filename(_stem):
            return None

    tpl = meta_null["pipe"].site.templates[T_LOG]
    human = pagemeta.human_values(clone_db(meta_null["pipe"].con), Site, "doc", 2, "pid", tpl)
    assert human["date"] == ("2030.01.07", "label") and "date.day" not in human and "date.month" not in human
    machine = {k: pagemeta.MachineRead("7", 0.99, "auto") for k in ("date.month", "date.day")}
    rows = {r["meta_key"]: r for r in pagemeta.resolve("pid", tpl, human, machine)}
    assert rows["date.day"]["check_result"] == "none" and rows["date"]["check_result"] == "none"
    # 날짜 대조: 자동 적재된 부분 하나라도 다르면 mismatch — 다른 부분(월)을 읽지 못했어도
    iso = {"date": ("2030-01-10", "filename"), "date.month": ("1", "filename"), "date.day": ("10", "filename")}
    R = pagemeta.MachineRead
    cases = [({"date.month": R(None, None, "empty"), "date.day": R("7", 0.99, "auto")}, "mismatch"),
             ({"date.month": R(None, None, "empty"), "date.day": R("10", 0.99, "auto")}, "unread"),
             ({"date.month": R("1", 0.99, "auto"), "date.day": R("10", 0.99, "auto")}, "match"),
             ({"date.month": R("1", 0.4, "pending"), "date.day": R("10", 0.99, "auto")}, "unread")]
    for machine, want in cases:
        assert {r["meta_key"]: r for r in pagemeta.resolve("pid", tpl, iso, machine)}["date"]["check_result"] == want
    run = meta_mislabeled
    res = run_regression(run["settings"], run["pipe"].site, update=True, inputs=[sorted(meta_synth.scans.glob("*.pdf"))[0].name])
    pm = res["report"]["page_meta"]["vehicle_no"]
    assert "machine" not in pm["by_source"] and pm["machine"] == {}


def test_crop_level_answers_equal_pipeline_values(meta_mislabeled, tmp_path):
    """같은 쪽이면 export-crops --meta → recognizer eval 의 답·신뢰도 = 파이프라인의 기계 값 (0003 과 같은 확인)."""
    from minedocscan.recognize.meta.evaluate import evaluate_meta as eval_crops
    from minedocscan.review.export import export_meta_crops

    pipe = meta_mislabeled["pipe"]
    con = clone_db(pipe.con)
    export_meta_crops(con, pipe.site, meta_mislabeled["settings"], tmp_path / "c")
    machine = {r["field_id"]: (r["machine_value"], r["machine_confidence"]) for r in con.execute(
        "SELECT * FROM doc_page_meta WHERE machine_status IS NOT NULL AND machine_status != 'empty'")}
    from conftest import META_FIXTURES

    n = 0
    for model, split in (("meta-digits", None), ("meta-operator", None)):
        r = eval_crops(tmp_path / "c", META_FIXTURES / model, split=split, site=pipe.site)
        for p in r["predictions"]:
            v, c = machine[p["field_id"]]
            assert p["value"] == v and abs(p["confidence"] - c) < 1e-9, p["field_id"]
            n += 1
    assert n >= 0.9 * len(machine)


def test_queues_eval_and_info(meta_truth, meta_nolabels, tmp_path, capsys):
    """page-fields 에는 기계가 채운 키가 나오지 않는다. --audit N 은 기계 값과 상관없이 날짜별로 고르게 뽑은 쪽 (기계 값 없이).
    eval --meta: 정답이 있는 쪽에서. info: 키마다 모델·기준."""
    pipe = meta_nolabels["pipe"]
    con = clone_db(pipe.con)
    site = pipe.site
    meta = _meta(con)
    q = build_queue(con, "page-fields", site=site)
    unfilled = {(pid, k) for (pid, k), r in meta.items() if k in ("vehicle_no", "operator") and r["value"] is None}
    shown = {(i["item_id"], c["meta_key"]) for i in q["items"] for c in i["cells"]}
    assert shown == unfilled and all(c["machine"] is None for i in q["items"] for c in i["cells"])
    a = build_queue(con, "page-fields", site=site, audit=8, seed=1)
    assert a["total"] == 8 and len(a["items"]) == 8 and a == build_queue(con, "page-fields", site=site, audit=8, seed=1)
    dates = [i["work_date"] for i in a["items"]]
    assert len(set(dates)) == 4 and max(dates.count(d) for d in dates) == 2               # 날짜별로 고르게
    assert all(len(i["cells"]) == 2 and c["machine"] is None for i in a["items"] for c in i["cells"])
    assert any(meta[(i["item_id"], "vehicle_no")]["source"] == "machine" for i in a["items"])     # 기계가 채운 쪽도 나온다
    # 표본을 정답대로 검수하면 eval --meta 가 그 쪽들에서 잰다
    settings = replace(meta_nolabels["settings"], reviews=tmp_path / "r.jsonl")
    src_of = {pid: s for s, pid in _log_pages(con).items()}
    for i in a["items"]:
        for c in i["cells"]:
            save(con, site, settings, Review(c["field_id"], "value", meta_truth[src_of[i["item_id"]]][c["meta_key"]], "jp"))
    a2 = build_queue(con, "page-fields", site=site, audit=8, seed=1)
    assert a2["done"] == 8 and a2["items"] == []
    r = evaluate_meta(con)
    v, o = r["keys"]["vehicle_no"], r["keys"]["operator"]
    assert v["n"] == o["n"] == 8 and v["auto_error"]["wrong"] <= 0 + (v["auto_error"]["auto"] // 50)
    assert r["slots"]["pages"] >= 7 and r["slots"]["same"] >= r["slots"]["pages"] - 1
    assert {"date.month", "date.day"} <= set(r["keys"]) and r["keys"]["date.day"]["accuracy"] >= 0.9
    s = meta_nolabels["settings"]
    cfg = tmp_path / "c.toml"
    from conftest import meta_options

    lines = ["[recognize.meta]"] + [f'"{k}" = {json.dumps(str(v))}' for k, v in meta_options()["meta"].items()]   # 윈도우 경로의 역슬래시
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["info", "--config", str(cfg), "--site", str(s.site), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)["meta_readers"]["by_key"]
    assert info["operator"]["reader"] == "choice" and info["vehicle_no"]["auto_accept_conf"] is not None
    assert main(["info", "--config", str(cfg), "--site", str(s.site)]) == 0
    assert "메타 필드 [operator]: 모델 meta-operator (choice" in capsys.readouterr().out
    # 다른 키에 꽂은 모델: 시작할 때 오류 (트레이스백 없이)
    cfg.write_text(f'[recognize.meta]\noperator = {json.dumps(str(meta_options()["meta"]["vehicle_no"]))}\n', encoding="utf-8")
    import pytest

    with pytest.raises(SystemExit, match="operator"):
        main(["run", str(s.archive_root), "--config", str(cfg), "--site", str(s.site), "--work-root", str(tmp_path / "w")])
