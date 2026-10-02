"""파이프라인 전체를 합성 3일치로 돌려, 결과를 '무엇을 적었는지'(정답)와 비교한다."""
from minedocscan.evaluate.fields import evaluate_fields
from minedocscan.recognize import load_answers_json
from minedocscan.report import build_report, xcheck_by_date
from minedocscan.tools.synth import SLOTS, T_INSP, T_LOG, UG_ROWS


def test_every_page_classified_aligned_and_loaded(synth, null_run):
    exp = synth.truth["expected"]
    rep = build_report(null_run.con)
    assert rep["documents"] == exp["documents"] and rep["pages"] == exp["pages"]
    assert rep["pages_by_form"] == exp["pages_by_form"]
    assert rep["pages_by_status"] == {"loaded": exp["pages"]}
    for name, a in rep["align"].items():
        assert a["ok"] == a["pages"], name
        assert a["max_grid_err"] <= 3.0, name
    assert null_run.summary["low_margin"] == []
    # 페이지 순서까지 정답과 같아야 한다
    for stem, pages in synth.truth["documents"].items():
        got = [r[0] for r in null_run.con.execute(
            "SELECT p.template_name FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
            "WHERE d.source_name = ? ORDER BY p.page_no", (stem,))]
        assert got == [p["template"] for p in pages]


def test_checkmarks_match_truth(synth, null_run):
    exp = synth.truth["expected"]["inspection"]
    rep = build_report(null_run.con)["inspection"]
    assert {k: rep[k] for k in exp} == exp
    truth = {(r["date"], r["row_key"]): r["abnormal"] for d in synth.truth["days"] for r in d["inspection"]}
    got = {(r[0], r[1]): (None if r[2] is None else bool(r[2])) for r in null_run.con.execute(
        "SELECT d.inspection_date, e.equipment_key, d.abnormal FROM insp_daily d "
        "JOIN eq_equipment e ON d.equipment_id = e.equipment_id")}
    assert got == truth


def test_haul_presence_matches_truth(synth, null_run):
    """인식기가 없어도 '어느 칸에 값이 적혔는가'는 정확해야 한다 (메모는 값이 아니다)."""
    con = null_run.con
    for d in synth.truth["days"]:
        want = {(r["slot"], r["material"], r["level"]) for r in d["haul_matrix"]}
        got = {tuple(r) for r in con.execute(
            "SELECT slot, material, level FROM prod_haul WHERE source_role='matrix' AND has_value=1 AND work_date=?",
            (d["date"],))}
        assert got == want, d["date"]
        want = {(r["slot"], r["material"], r["level"], r["shift"]) for r in d["haul_log"]}
        got = {tuple(r) for r in con.execute(
            "SELECT slot, material, level, shift FROM prod_haul WHERE source_role='log' AND has_value=1 AND work_date=?",
            (d["date"],))}
        assert got == want, d["date"]


def test_crosscheck_without_recognizer(synth, null_run):
    exp = synth.truth["expected"]
    rep = build_report(null_run.con)
    assert rep["xcheck_haul"] == exp["xcheck_haul_has_only"]
    assert rep["assignments"] == exp["assignments"]
    # 행렬에 빠뜨린 칸이 정확히 불일치로 잡혀야 한다
    want = {(d["date"], x["slot"], x["material"], x["level"]) for d in synth.truth["days"]
            for x in d["discrepancies"] if x["type"] == "missing_in_matrix"}
    got = {tuple(r) for r in null_run.con.execute(
        "SELECT work_date, slot, material, level FROM xcheck_haul WHERE status='mismatch'")}
    assert got == want
    by_date = {d["work_date"]: d for d in xcheck_by_date(null_run.con)}
    assert by_date[synth.truth["days"][2]["date"]]["missing_log"] == len(UG_ROWS)       # 일보를 안 낸 차량


def test_assignment_observations(synth, null_run):
    got = {(r["work_date"], r["slot"]): (r["vehicle_no"], r["operator"], r["matched_by"])
           for r in null_run.con.execute("SELECT * FROM eq_assignment_obs")}
    want = {(d["date"], t["slot"]): (t["vehicle_no"], t["operator"], t["matched_by"])
            for d in synth.truth["days"] for t in d["trucks"] if t["has_log"]}
    assert got == want
    assert len({s for s, _v, _o in SLOTS}) == 4


def test_null_backend_sends_handwriting_to_review(null_run):
    con = null_run.con
    # 잉크가 있는 수기 셀은 전부 검수 대기, 빈 셀은 자동 확정
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE kind LIKE 'handwritten%' AND has_value=1 "
                       "AND review_status='auto'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE kind LIKE 'handwritten%' AND has_value=0 "
                       "AND review_status!='auto'").fetchone()[0] == 0
    assert {r[0] for r in con.execute("SELECT DISTINCT status FROM doc_document")} == {"needs_review"}
    assert con.execute("SELECT COUNT(*) FROM prod_haul WHERE trips IS NOT NULL").fetchone()[0] == 0


def test_provenance_is_kept(null_run):
    """모든 값은 출처(페이지·좌표)를 가진다 — 정합 이미지에서 그 자리를 다시 볼 수 있어야 한다."""
    con = null_run.con
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE x1 <= x0 OR y1 <= y0 OR page_id IS NULL").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM prod_haul h LEFT JOIN doc_field f ON h.source_field_id = f.field_id "
                       "WHERE f.field_id IS NULL").fetchone()[0] == 0
    for (rel,) in con.execute("SELECT aligned_image FROM doc_page"):
        assert (null_run.settings.work_root / rel).exists()


def test_rerun_is_idempotent(synth, null_run):
    before = build_report(null_run.con)
    first = sorted(synth.scans.glob("*.pdf"))[0]
    null_run.process_file(first)
    null_run.finalize()
    assert build_report(null_run.con) == before


def test_oracle_backend_gives_zero_error(synth, oracle_run):
    """정답을 돌려주는 인식기로 돌리면 오류가 0 이어야 한다. 아니면 인식기가 아니라 파이프라인의 버그다."""
    res = evaluate_fields(oracle_run.con, load_answers_json(synth.answers_path))
    assert res["cer"] == 0.0 and res["field_accuracy"] == 1.0
    assert res["answers_not_in_db"] == 0          # 모든 정답이 DB 의 어떤 셀과 짝지어졌다
    assert res["n"] > len(load_answers_json(synth.answers_path))      # 정답에 없는 빈 칸도 평가에 들어간다
    assert res["auto_rate"] == 1.0
    assert set(res["by_field_kind"]) == {f"{T_INSP}/handwritten_text", f"{T_LOG}/handwritten_number",
                                         "synth_haul_matrix/handwritten_number"}


def test_oracle_trips_and_crosscheck(synth, oracle_run):
    con = oracle_run.con
    exp = synth.truth["expected"]
    assert build_report(con)["xcheck_haul"] == exp["xcheck_haul_with_trips"]
    for d in synth.truth["days"]:
        want = {(r["slot"], r["material"], r["level"]): r["trips"] for r in d["haul_matrix"]}
        got = {(r[0], r[1], r[2]): r[3] for r in con.execute(
            "SELECT slot, material, level, trips FROM prod_haul WHERE source_role='matrix' AND has_value=1 "
            "AND work_date=?", (d["date"],))}
        assert got == want
    # 횟수가 다른 칸은 숫자를 읽어야만 드러난다
    want = {(d["date"], x["slot"], x["material"], x["level"]) for d in synth.truth["days"] for x in d["discrepancies"]}
    got = {tuple(r) for r in con.execute("SELECT work_date, slot, material, level FROM xcheck_haul WHERE status='mismatch'")}
    assert got == want


def test_oracle_inspection_rows(synth, oracle_run):
    rep = build_report(oracle_run.con)["inspection"]
    exp = synth.truth["expected"]["inspection"]
    # 체크가 판정된 행은 전부 자동 적재, 점검을 하지 않은 날의 행만 검수로 간다
    assert rep["auto"] == exp["abnormal_yes"] + exp["abnormal_no"]
    truth = {(r["date"], r["row_key"]): r["remark"] for d in synth.truth["days"] for r in d["inspection"]}
    got = {(r[0], r[1]): r[2] or "" for r in oracle_run.con.execute(
        "SELECT d.inspection_date, e.equipment_key, d.remark FROM insp_daily d "
        "JOIN eq_equipment e ON d.equipment_id = e.equipment_id")}
    assert got == truth
    iso = dict(oracle_run.con.execute("SELECT site_category, iso_type FROM eq_equipment").fetchall())
    assert iso["Drill"] == "Drill" and iso["Charger"] is None
