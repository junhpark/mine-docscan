"""쪽 메타의 출처 (tasks/0004 단계 2): doc_page_meta, 날짜의 월·일 필드, 메타 필드 크롭 내보내기, 합성 메타 필드."""
import json
from dataclasses import replace

import numpy as np
import pytest

from conftest import clone_db
from minedocscan.forms.template import Template, TemplateError
from minedocscan.pipeline import Pipeline
from minedocscan.report import build_report
from minedocscan.review.export import export_meta_crops
from minedocscan.review.store import Review, field_id_of, save
from minedocscan.tools import synth_meta
from minedocscan.tools.synth import T_LOG
from test_review_store import TABLES, _dump


def _meta(con, page_id=None) -> dict:
    sql, args = "SELECT * FROM doc_page_meta", ()
    if page_id:
        sql, args = sql + " WHERE page_id = ?", (page_id,)
    return {(r["page_id"], r["meta_key"]): dict(r) for r in con.execute(sql, args)}


def _log_pages(con) -> dict:
    return {f"{r['source_name']}#{r['page_no']}": r["page_id"] for r in con.execute(
        "SELECT p.page_id, p.page_no, d.source_name FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
        "WHERE p.template_name = ? ORDER BY 1", (T_LOG,))}


def test_sources_of_page_meta(meta_synth, meta_null):
    """라벨 → label, 파일명의 날짜 → filename, 날짜의 월·일은 그 날짜에서. 읽는 모델이 없으면 기계 열은 비고 대조는 none."""
    con = meta_null["pipe"].con
    labels = json.loads((meta_synth.site / "labels" / "pages.json").read_text(encoding="utf-8"))
    pages = _log_pages(con)
    assert len(pages) == len(labels) and pages
    meta = _meta(con)
    for source, pid in pages.items():
        date = con.execute("SELECT work_date FROM doc_page WHERE page_id=?", (pid,)).fetchone()[0]
        _y, m, d = date.split("-")
        want = {"date": (date, "filename"), "date.month": (str(int(m)), "filename"), "date.day": (str(int(d)), "filename"),
                "vehicle_no": (labels[source]["vehicle_no"], "label"), "operator": (labels[source]["operator"], "label")}
        for k, (v, src) in want.items():
            row = meta[(pid, k)]
            assert (row["value"], row["source"]) == (v, src), (source, k)
            assert row["machine_status"] is None and row["check_result"] == "none"
        assert meta[(pid, "vehicle_no")]["field_id"] == field_id_of(pid, "vehicle_no")
        assert meta[(pid, "date")]["field_id"] is None
    # 점검표 쪽에는 날짜 한 줄만 (메타 필드가 없다)
    insp = con.execute("SELECT page_id FROM doc_page WHERE template_name = 'synth_inspection'").fetchall()
    assert insp and all({k for (p, k) in meta if p == r[0]} == {"date"} for r in insp)
    # 일보의 차량·작성자는 이 테이블의 최종 값이다
    for _source, pid in pages.items():
        veh = {r[0] for r in con.execute("SELECT DISTINCT vehicle_no FROM prod_haul WHERE page_id=?", (pid,))}
        assert veh <= {meta[(pid, "vehicle_no")]["value"]}


def test_date_parts_are_read_only(meta_synth, tmp_path):
    spec = Template(meta_synth.site / "templates" / T_LOG / "template.yaml")
    assert spec.meta_fields()["date_day"] == "date.day" and "date_day" not in spec.review_meta_fields()
    import yaml

    raw = dict(spec.spec)
    for bad, pat in (("date.year", "날짜의 부분"), ("date", "date")):
        raw["fields"] = [dict(spec.spec["fields"][0], meta_key=bad)]
        (tmp_path / "template.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        with pytest.raises(TemplateError, match=pat):
            Template(tmp_path / "template.yaml")
    raw["fields"] = [dict(f, meta_key="operator") for f in spec.spec["fields"][:2]]
    (tmp_path / "template.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(TemplateError, match="같은 meta_key"):
        Template(tmp_path / "template.yaml")


def test_reviews_take_over_from_empty_labels(meta_synth, meta_null, tmp_path):
    """라벨을 비우고 같은 값을 검수로 넣으면 최종 값은 같고 출처만 review. 날짜의 월·일 검수는 메타를 바꾸지 않는다.
    불변식: 저장 직후의 DB == 같은 검수 파일로 새로 돌린 DB (doc_page_meta 포함)."""
    import shutil

    site_dir = tmp_path / "site"
    shutil.copytree(meta_synth.site, site_dir)
    labels = json.loads((site_dir / "labels" / "pages.json").read_text(encoding="utf-8"))
    (site_dir / "labels" / "pages.json").write_text("{}", encoding="utf-8")
    settings = replace(meta_null["settings"], site=site_dir, work_root=tmp_path / "w1", reviews=tmp_path / "r.jsonl")
    one_day = sorted(meta_synth.scans.glob("*.pdf"))[1]
    live = Pipeline(settings)
    live.run([one_day])
    con = live.con
    pages = _log_pages(con)
    assert pages and all(_meta(con, p)[(p, "vehicle_no")]["value"] is None for p in pages.values())
    assert build_report(con)["assignments"]["n"] == 0
    k = 0
    for source, pid in pages.items():
        for key in ("vehicle_no", "operator"):
            save(con, live.site, settings, Review(field_id_of(pid, key), "value", labels[source][key], "jp",
                                                  reviewed_at=f"2030-03-01T00:00:{k:02d}Z"))
            k += 1
    # 날짜의 월·일 칸에 엉뚱한 값을 검수해도 날짜(와 그 부분)는 파일명에서 온다
    pid0 = next(iter(pages.values()))
    save(con, live.site, settings, Review(field_id_of(pid0, "date_day"), "value", "31", "jp", reviewed_at="2030-03-01T00:01:00Z"))
    after = _meta(con)
    for source, pid in pages.items():
        for key in ("vehicle_no", "operator"):
            assert (after[(pid, key)]["value"], after[(pid, key)]["source"]) == (labels[source][key], "review")
    assert after[(pid0, "date.day")]["source"] == "filename" and after[(pid0, "date.day")]["value"] != "31"
    # 라벨이 있는 실행과 업무 테이블이 같다
    labeled = Pipeline(replace(meta_null["settings"], work_root=tmp_path / "w_lab", reviews=tmp_path / "none.jsonl"))
    labeled.run([one_day])
    for t in ("prod_haul", "xcheck_haul", "eq_assignment_obs"):
        assert _dump(labeled.con, t) == _dump(con, t), t
    fresh = Pipeline(replace(settings, work_root=tmp_path / "w2"))
    fresh.run([one_day])
    assert build_report(fresh.con) == build_report(con)
    for t in TABLES:
        assert _dump(fresh.con, t) == _dump(con, t), t
    # 검수에서 빈 칸이면 값이 없어진다 (라벨이 있어도 지운다)
    save(con, live.site, settings, Review(field_id_of(pid0, "operator"), "empty", reviewer="jp", reviewed_at="2030-03-01T00:02:00Z"))
    row = _meta(con, pid0)[(pid0, "operator")]
    assert row["value"] is None and row["source"] is None
    assert {r[0] for r in con.execute("SELECT DISTINCT operator FROM prod_haul WHERE page_id=?", (pid0,))} == {None}


def test_export_meta_crops(meta_synth, meta_null, tmp_path):
    """줄 수 = PNG 수 = 사람·파일명 값이 있는 (쪽, 키) 수. test 와 train 이 섞이지 않는다. 기계 값은 정답이 아니다."""
    con = clone_db(meta_null["pipe"].con)
    pipe = meta_null["pipe"]
    want = con.execute("SELECT COUNT(*) FROM doc_page_meta WHERE field_id IS NOT NULL AND value IS NOT NULL "
                       "AND source IN ('review', 'label', 'filename')").fetchone()[0]
    out = export_meta_crops(con, pipe.site, meta_null["settings"], tmp_path / "meta", res="aligned")
    lines = [json.loads(x) for sp in out["by_split"] for x in
             (tmp_path / "meta" / sp / "meta" / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    pngs = list((tmp_path / "meta").rglob("*.png"))
    assert out["written"] == len(lines) == len(pngs) == want > 0
    assert set(out["by_key"]) == set(synth_meta.META_KEYS) and set(out["by_label_source"]) == {"label", "filename"}
    for ln in lines:
        assert ln["split"] == pipe.site.split_of(ln["work_date"]) and ln["file"].startswith(f"{ln['split']}/meta/")
        assert ln["spec"] == {"res": "aligned", "scale": 1.5, "pad": 8}
        assert ln["label_source"] in ("label", "filename") and ln["verdict"] == "value"
    # 한 키만, 한 분할만
    one = export_meta_crops(con, pipe.site, meta_null["settings"], tmp_path / "op", meta_key="operator", split="train",
                            res="aligned")
    assert set(one["by_key"]) == {"operator"} and set(one["by_split"]) <= {"train"}
    # illegible 로 검수된 필드는 --include-illegible 일 때만
    pid = next(iter(_log_pages(con).values()))
    settings = replace(meta_null["settings"], reviews=tmp_path / "r.jsonl")
    save(con, pipe.site, settings, Review(field_id_of(pid, "operator"), "illegible", reviewer="jp"))
    a = export_meta_crops(con, pipe.site, settings, tmp_path / "a", meta_key="operator", res="aligned")
    b = export_meta_crops(con, pipe.site, settings, tmp_path / "b", meta_key="operator", res="aligned", include_illegible=True)
    assert a["written"] == b["written"] - 1 and a["skipped_illegible"] == 1
    # 저장소 안에는 쓰지 않는다
    from pathlib import Path

    from minedocscan.review.export import ExportError

    with pytest.raises(ExportError, match="git"):
        export_meta_crops(con, pipe.site, settings, Path(__file__).parent / "_meta_out")


def test_cell_training_ignores_meta_lines(meta_null, tmp_path):
    """숫자 칸 학습(read_crops)은 메타 필드 줄을 읽지 않는다 — 같은 OUT 에 내보내도 섞이지 않는다."""
    from minedocscan.recognize.digits import data

    pipe = meta_null["pipe"]
    export_meta_crops(clone_db(pipe.con), pipe.site, meta_null["settings"], tmp_path / "o", res="aligned")
    crops = data.read_crops(tmp_path / "o", allow_test=True)
    assert crops.samples == [] and crops.skipped["meta_field"] == crops.lines > 0
    meta = data.read_crops(tmp_path / "o", allow_test=True, meta_keys=("date.day",))
    assert meta.samples and meta.skipped["other_key"] > 0


def _fingerprint() -> np.ndarray:
    """정해진 씨앗의 합성 메타 필드 세 개를 8×32 로 줄인 것 (이름, 차량번호, 일)."""
    import cv2

    out = []
    for seed, text, kind, w in ((1, "ALPHA", "name", 370), (2, "4127", "digits", 390), (3, "17", "digits", 140)):
        rng = np.random.default_rng(seed)
        st = synth_meta.page_style(synth_meta.writer_style("ALPHA" if kind == "name" else "BRAVO"), rng)
        img = synth_meta.make_field_crop(rng, text, kind, synth_meta.FieldSpec(w, 65), st)
        out.append(cv2.resize(img, (32, 8), interpolation=cv2.INTER_AREA).astype(int))
    return np.array(out)


def test_synthetic_meta_fields_do_not_depend_on_the_opencv_version():
    """같은 씨앗의 합성 메타 필드가 OpenCV 판과 무관하게 거의 같다 (tasks/0004 4.8). 기준은 OpenCV 5.0 에서 만든 것 —
    하한 판(CI lowest, 4.9)에서도 같은 시험이 돈다. 4.14 에서 최대 차이 1, 평균 0.03 단계였다."""
    from pathlib import Path

    ref = np.array(json.loads((Path(__file__).parent / "fixtures" / "synth_meta_fingerprint.json").read_text()))
    got = _fingerprint()
    assert got.shape == ref.shape
    assert np.abs(got - ref).max() <= 3 and np.abs(got - ref).mean() < 0.5
    assert (_fingerprint() == got).all()                                   # 같은 씨앗이면 같은 화소


def test_meta_synth_pages_carry_what_the_scenarios_say(meta_synth, synth):
    truth = meta_synth.truth
    assert truth["meta_fields"] is True and "meta_fields" not in synth.truth          # 기본 합성 데이터는 그대로
    scen = [s for d in truth["days"] for s in d["scenarios"]]
    assert {"T03_T04_swapped_vehicles", "T02_new_vehicle", "T04_new_operator"} <= set(scen)
    labels = json.loads((meta_synth.site / "labels" / "pages.json").read_text(encoding="utf-8"))
    assert all(v["vehicle_no"].isdigit() and len(v["vehicle_no"]) == 4 for v in labels.values())
    assert synth_meta.STRANGERS[0] in {v["operator"] for v in labels.values()}
