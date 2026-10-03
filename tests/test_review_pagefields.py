"""페이지 필드(차량번호·작성자) 검수 — 수동 라벨 대체. 메타 우선순위: 검수값 > 페이지 라벨 > 문서 라벨 > 파일명."""
import json
from dataclasses import replace

import pytest

from minedocscan.config import Settings
from minedocscan.forms.template import Template, TemplateError
from minedocscan.pipeline import Pipeline
from minedocscan.report import build_report
from minedocscan.review.queue import build_queue
from minedocscan.review.server import ReviewApp
from minedocscan.review.store import Review, field_id_of, save
from minedocscan.tools.synth import SLOTS, T_LOG, expected_xcheck, generate

TABLES = ("doc_field", "prod_haul", "insp_daily", "xcheck_haul", "eq_assignment_obs", "doc_document")


def _dump(con, table):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})") if r[1] != "created_at"]
    return sorted(tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {table}"))


@pytest.fixture(scope="module")
def nolabels(tmp_path_factory):
    """라벨을 비운 하루치: 일보 쪽의 차량번호·작성자를 모르는 상태로 돌린다."""
    root = tmp_path_factory.mktemp("pagefields")
    synth = generate(root / "data", days=1, seed=0)
    labels_path = synth.site / "labels" / "pages.json"
    truth_labels = json.loads(labels_path.read_text(encoding="utf-8"))
    labels_path.write_text("{}", encoding="utf-8")
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=root / "work",
                        reviews=root / "검수" / "reviews.jsonl")
    pipe = Pipeline(settings)
    pipe.run([synth.scans])
    return {"synth": synth, "settings": settings, "pipe": pipe, "labels": truth_labels, "root": root}


def _log_pages(con):
    return {f"{r['source_name']}#{r['page_no']}": r["page_id"] for r in con.execute(
        "SELECT p.page_id, p.page_no, d.source_name FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
        "WHERE p.template_name = ?", (T_LOG,))}


def test_meta_key_validation(tmp_path, synth):
    spec = Template(synth.site / "templates" / T_LOG / "template.yaml").spec
    assert Template(synth.site / "templates" / T_LOG / "template.yaml").meta_fields() == {"vehicle_no": "vehicle_no",
                                                                                            "operator": "operator"}
    import yaml

    bad = dict(spec)
    bad["fields"] = [dict(spec["fields"][0], meta_key="date")]
    (tmp_path / "template.yaml").write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    with pytest.raises(TemplateError, match="date"):
        Template(tmp_path / "template.yaml")


def test_reviews_replace_labels(nolabels):
    con, site, settings, synth = nolabels["pipe"].con, nolabels["pipe"].site, nolabels["settings"], nolabels["synth"]
    day = synth.truth["days"][0]
    before = build_report(con)
    assert before["assignments"]["n"] == 0                                   # 라벨이 없으면 자리를 못 정한다
    q = build_queue(con, "page-fields", site=site)
    pages = _log_pages(con)
    assert q["total"] == len(pages) == len(q["items"]) and q["done"] == 0
    assert all(len(i["cells"]) == 2 and {c["meta_key"] for c in i["cells"]} == {"vehicle_no", "operator"} for i in q["items"])
    assert set(q["candidates"]) == {"vehicle_no", "operator"}
    assert set(v for _s, v, _o in SLOTS) <= set(q["candidates"]["vehicle_no"])       # 행렬 머리글이 후보에 있다
    assert all(c["machine"] is None for i in q["items"] for c in i["cells"])

    # 한 쪽은 차량번호만 먼저 → 남은 필드만 가지고 다시 나온다
    first_source, first_page = next(iter(pages.items()))
    save(con, site, settings, Review(field_id_of(first_page, "vehicle_no"), "value",
                                     nolabels["labels"][first_source]["vehicle_no"], "jp"))
    q2 = build_queue(con, "page-fields", site=site)
    item = next(i for i in q2["items"] if i["item_id"] == first_page)
    assert [c["meta_key"] for c in item["cells"]] == ["operator"] and q2["done"] == 0

    # 전부 입력하면 라벨이 있을 때와 같다
    for source, page_id in pages.items():
        for key in ("vehicle_no", "operator"):
            if (source, key) == (first_source, "vehicle_no"):
                continue
            save(con, site, settings, Review(field_id_of(page_id, key), "value", nolabels["labels"][source][key], "jp"))
    rep = build_report(con)
    assert rep["xcheck_haul"] == expected_xcheck([day], with_trips=False)
    assert rep["assignments"] == synth.truth["expected"]["assignments"]
    got = {r["slot"]: (r["vehicle_no"], r["operator"]) for r in con.execute("SELECT * FROM eq_assignment_obs")}
    assert got == {t["slot"]: (t["vehicle_no"], t["operator"]) for t in day["trucks"] if t["has_log"]}
    q3 = build_queue(con, "page-fields", site=site)
    assert q3["items"] == [] and q3["done"] == q3["total"] == len(pages)
    # 후보 목록에 검수값이 들어간다 (많이 나온 순)
    assert q3["candidates"]["operator"][0] in {t["operator"] for t in day["trucks"]}


def test_rebuild_matches_live_db_with_page_fields(nolabels, tmp_path):
    """불변식: 페이지 필드를 저장한 직후의 DB == 같은 검수 파일로 새로 돌린 DB."""
    live = nolabels["pipe"].con
    fresh = Pipeline(replace(nolabels["settings"], work_root=tmp_path / "work2"))
    fresh.run([nolabels["synth"].scans])
    assert build_report(fresh.con) == build_report(live)
    for t in TABLES:
        assert _dump(fresh.con, t) == _dump(live, t), t


def test_review_beats_label_and_any_value_is_accepted(nolabels):
    con, site, settings, synth = nolabels["pipe"].con, nolabels["pipe"].site, nolabels["settings"], nolabels["synth"]
    pages = _log_pages(con)
    source, page_id = next(iter(pages.items()))
    # 라벨에 틀린 차량번호를 넣어도 검수값이 이긴다
    labels_path = synth.site / "labels" / "pages.json"
    labels_path.write_text(json.dumps({source: {"vehicle_no": "V-000", "operator": "NOBODY"}}), encoding="utf-8")
    site._labels = None
    from minedocscan.review.store import page_meta

    meta = page_meta(con, site, source.split("#")[0], int(source.split("#")[1]), page_id, site.templates[T_LOG])
    assert meta["vehicle_no"] == nolabels["labels"][source]["vehicle_no"] and meta["operator"] != "NOBODY"
    labels_path.write_text("{}", encoding="utf-8")
    site._labels = None
    # 후보에 없는 값도 서버는 받는다 (확인은 화면의 일)
    app = ReviewApp(con, site, settings, "jp", "page-fields")
    out = app.post_review({"field_id": field_id_of(page_id, "vehicle_no"), "verdict": "value", "value": "V-999"})
    assert out["ok"] and "V-999" in build_queue(con, "page-fields", site=site)["candidates"]["vehicle_no"]
    assert con.execute("SELECT DISTINCT vehicle_no FROM prod_haul WHERE page_id=?", (page_id,)).fetchone()[0] == "V-999"
    # 되돌려 둔다 (다른 테스트의 불변식과 무관하게 파일에는 두 줄 다 남는다)
    save(con, site, settings, Review(field_id_of(page_id, "vehicle_no"), "value", nolabels["labels"][source]["vehicle_no"], "jp"))
