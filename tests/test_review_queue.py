"""대기열: 표본의 결정성과 구성, 불일치 묶음, 검수된 필드 제외."""
import pytest

from conftest import run_day
from minedocscan.review.queue import QUEUES, build_queue
from minedocscan.review.store import Review, save
from minedocscan.tools.synth import T_LOG


@pytest.fixture(scope="module")
def day(tmp_path_factory):
    synth, settings, pipe = run_day(tmp_path_factory.mktemp("review_queue"), seed=1)
    return {"synth": synth, "settings": settings, "pipe": pipe}


def _ids(q):
    return [c["field_id"] for i in q["items"] for c in i["cells"]]


def test_sample_is_deterministic_and_stratified(null_run):
    con = null_run.con
    a = build_queue(con, "haul-numbers", n=60, seed=7, empty_share=0.1)
    b = build_queue(con, "haul-numbers", n=60, seed=7, empty_share=0.1)
    assert _ids(a) == _ids(b) and a["total"] == 60 and a["done"] == 0
    assert _ids(a) != _ids(build_queue(con, "haul-numbers", n=60, seed=8, empty_share=0.1))
    # 빈 칸 비율: 60 × 0.1 = 6
    raw = dict(con.execute("SELECT field_id, has_value_raw FROM doc_field"))
    assert sum(raw[f] == 0 for f in _ids(a)) == 6 and sum(raw[f] == 1 for f in _ids(a)) == 54
    # 날짜 × 역할 층이 고르게 (3일 × 2역할 = 6층 → 값 있는 54개는 층마다 9개)
    role = dict(con.execute("SELECT source_field_id, work_date || '/' || source_role FROM prod_haul"))
    counts: dict = {}
    for f in _ids(a):
        if raw[f] == 1:
            counts[role[f]] = counts.get(role[f], 0) + 1
    assert set(counts.values()) == {9} and len(counts) == 6
    # 순서: 날짜 → 쪽 → 행 → 열
    assert [i["work_date"] for i in a["items"]] == sorted(i["work_date"] for i in a["items"])
    # 모집단이 모자라면 있는 만큼
    big = build_queue(con, "haul-numbers", n=100000, seed=7, empty_share=0.5)
    n_all = con.execute("SELECT COUNT(*) FROM prod_haul").fetchone()[0]
    assert big["total"] == n_all
    # 기계 값은 숨긴다
    assert all(c["machine"] is None for i in a["items"] for c in i["cells"])


def test_mismatch_items_bundle_both_documents(null_run):
    con = null_run.con
    q = build_queue(con, "mismatch")
    n = con.execute("SELECT COUNT(*) FROM xcheck_haul WHERE status='mismatch'").fetchone()[0]
    assert len(q["items"]) == n == q["total"] and n > 0
    for item in q["items"]:
        roles = {c["label"].split()[0] for c in item["cells"]}
        assert roles == {"일보", "행렬"}, item["title"]
        assert len(item["cells"]) == 3                       # 주간 + 야간 + 행렬 한 장
        assert all(c["machine"] is None for c in item["cells"])


def test_pending_lists_only_input_kinds_and_shows_machine(null_run):
    con = null_run.con
    q = build_queue(con, "pending")
    kinds = {c["kind"] for i in q["items"] for c in i["cells"]}
    assert kinds <= {"handwritten_number", "handwritten_text"}
    assert all(c["machine"] is not None for i in q["items"] for c in i["cells"])
    n = con.execute("SELECT COUNT(*) FROM doc_field WHERE review_status='pending' AND kind LIKE 'handwritten%'").fetchone()[0]
    assert len(q["items"]) == n
    only_log = build_queue(con, "pending", template=T_LOG, kind="handwritten_number")
    assert 0 < len(only_log["items"]) < n
    assert all(i["title"].split(" · ")[1] == T_LOG for i in only_log["items"])
    with pytest.raises(KeyError):
        build_queue(con, "nope")
    assert set(QUEUES) == {"haul-numbers", "mismatch", "pending"}


def test_reviewed_and_illegible_fields_leave_every_queue(day):
    con, settings, site = day["pipe"].con, day["settings"], day["pipe"].site
    before = build_queue(con, "haul-numbers", n=40, seed=3)
    ids = _ids(before)
    mm = build_queue(con, "mismatch")
    cell_in_mm = mm["items"][0]["cells"][0]["field_id"]
    pend = _ids(build_queue(con, "pending"))

    save(con, site, settings, Review(ids[0], "value", "4", "jp"))
    save(con, site, settings, Review(ids[1], "illegible", reviewer="jp"))
    save(con, site, settings, Review(cell_in_mm, "empty", reviewer="jp"))

    after = build_queue(con, "haul-numbers", n=40, seed=3)
    assert set(_ids(after)) == set(ids) - {ids[0], ids[1]}             # 원래 표본의 부분집합, 순서 유지
    assert _ids(after) == [f for f in ids if f not in (ids[0], ids[1])]
    assert after["total"] == 40 and after["done"] == 2
    assert ids[0] not in _ids(build_queue(con, "pending")) and ids[1] not in _ids(build_queue(con, "pending"))
    assert set(pend) - {ids[0], ids[1], cell_in_mm} == set(_ids(build_queue(con, "pending")))
    # 묶음의 일부만 검수되면 항목은 남고 그 셀에 기존 검수가 보인다; 전부 검수되면 빠진다
    mm2 = build_queue(con, "mismatch")
    item = next(i for i in mm2["items"] if any(c["field_id"] == cell_in_mm for c in i["cells"]))
    assert next(c for c in item["cells"] if c["field_id"] == cell_in_mm)["review"]["verdict"] == "empty"
    for c in item["cells"]:
        if c["field_id"] != cell_in_mm:
            save(con, site, settings, Review(c["field_id"], "value", "1", "jp"))
    mm3 = build_queue(con, "mismatch")
    assert all(cell_in_mm not in [c["field_id"] for c in i["cells"]] for i in mm3["items"])
    assert mm3["done"] + len(mm3["items"]) == mm3["total"]
