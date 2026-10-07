"""다시 스캔한 쪽 (tasks/0007 단계 3, 4.6): 서명, 붙잡기, 처리 순서와 무관함, 사람의 세 선택.

합성 데이터만 — rescan_synth(synth --rescans: 첫날 하루치와 그 쪽 몇 장을 다른 흔들기로 다시 찍은 파일 — 하나는 JPEG 재압축,
하나는 90° 돌려서). 렌더링·분류·정합은 저장해 둔 결과를 쓴다 (conftest.fast_imaging).
"""
from __future__ import annotations

import shutil
from dataclasses import replace

import numpy as np
import pytest

from conftest import fast_imaging, split_pages
from minedocscan.forms.sitepack import SitePack
from minedocscan.imaging.signature import decode, encode, signature, similarity
from minedocscan.intake import decisions as decs
from minedocscan.pipeline import Pipeline
from minedocscan.report import build_report
from test_reprocess import BUSINESS, RECEIVED, assert_same, doc_ids, dump, no_null_dates, settings_for


# ── 서명 (순수 함수) ───────────────────────────────────────────────────────
def test_signature_roundtrip_and_similarity_rules():
    rng = np.random.default_rng(0)
    page = np.full((160, 320), 245, np.uint8)
    for _ in range(12):                                          # 손글씨처럼 굵은 점
        y, x = int(rng.integers(10, 150)), int(rng.integers(10, 310))
        page[y - 3:y + 3, x - 3:x + 3] = 30
    mask = np.zeros(page.shape, bool)
    s = signature(page, mask)
    assert s.shape == (10, 20) and s.dtype == np.uint8 and s.sum() > 0
    assert np.array_equal(decode(encode(s)), s)
    assert similarity(s, s) == pytest.approx(1.0)
    assert similarity(s, np.zeros_like(s)) is None                # 전부 0 이면 비교하지 않는다
    assert similarity(s, s[:, :10]) is None                       # 다른 기준 그림
    assert signature(page, np.ones(page.shape, bool)).sum() == 0  # 지운 자리는 0
    shifted = np.roll(page, 64, axis=1)                           # 자리가 다르면 낮다
    assert similarity(s, signature(shifted, mask)) < 0.5


def test_signature_mask_covers_print_and_free_fields(site):
    """서명에서 지우는 자리: 인쇄(인쇄 층이 없으면 기준 이미지)와 표 밖 필드 — 같은 날 쪽마다 같은 날짜를 같은 자리에 쓴다."""
    tpl = site.templates["synth_haul_log"]
    m = tpl.signature_mask
    assert m.shape == tpl.reference.shape
    for f in tpl.spec["fields"]:
        x0, y0, x1, y1 = f["bbox"]
        assert m[y0:y1, x0:x1].all(), f["name"]
    assert signature(tpl.reference, m).sum() == 0                 # 기준 이미지(빈 양식)는 서명이 0
    assert tpl.sig_family == tpl.name


# ── 파이프라인 ─────────────────────────────────────────────────────────────
@pytest.fixture
def held(rescan_synth, tmp_path, monkeypatch):
    """첫날의 넉 장(점검표·T01·T02·행렬)을 담은 x, 그 넉 장을 다시 찍은 y(뒤 순서), T03 일보 하나인 o. 사이트 팩은 rescan_synth 의 것."""
    fast_imaging(monkeypatch)
    first, rescan = sorted(rescan_synth.scans.glob("scan_*.pdf"))
    scans = tmp_path / "scans"
    split_pages(first, [1, 2, 3, 6], scans / "x_2030-01-07.pdf")
    shutil.copyfile(rescan, scans / "y_2030-01-07.pdf")
    split_pages(first, [4], scans / "o_2030-01-07.pdf")
    st = settings_for(tmp_path, rescan_synth.site, scans)
    return {"st": st, "site": SitePack(rescan_synth.site), "scans": scans, "root": tmp_path, "truth": rescan_synth.truth}


def run(h, name="live", files=None) -> Pipeline:
    p = Pipeline(replace(h["st"], work_root=h["root"] / name), site=h["site"])
    p.run(files or [h["scans"]])
    return p


def pages_of(con, doc) -> dict:
    return {r["page_no"]: dict(r) for r in con.execute("SELECT * FROM doc_page WHERE document_id = ?", (doc,))}


def test_rescanned_pages_are_held_after_the_original(held):
    """다른 흔들기·JPEG 재압축·90° 돌린 다시 스캔이 전부 붙잡히고 duplicate_of 가 앞의 쪽이다. 업무 행은 한 번만 있다.
    같은 날의 다른 종이(T01·T02·T03 일보)는 붙잡히지 않는다."""
    p = run(held)
    ids = doc_ids(p.con)
    x, y = ids["x_2030-01-07"], ids["y_2030-01-07"]
    of = {1: 1, 2: 2, 3: 3, 4: 4}                                 # y 의 쪽 → x 의 쪽 (x 에 첫날의 1·2·3·6 쪽을 담았다)
    py = pages_of(p.con, y)
    assert [t["how"] for t in held["truth"]["rescans"]] == ["shake", "jpeg", "rotated", "shake"]
    for n, page in py.items():
        assert (page["status"], page["duplicate_of"]) == ("duplicate", f"{x}-p{of[n]}"), n
        assert page["duplicate_sim"] >= 0.99
    assert py[3]["rotation"] == 90
    assert {r["status"] for r in pages_of(p.con, x).values()} == {"loaded"}
    assert {r["status"] for r in pages_of(p.con, ids["o_2030-01-07"]).values()} == {"loaded"}
    assert p.con.execute("SELECT status FROM doc_document WHERE document_id = ?", (y,)).fetchone()[0] == "needs_review"
    for t in ("doc_field", "doc_page_meta", "doc_page_sig", "prod_haul", "prod_tally", "eq_usage_daily", "insp_daily",
              "xcheck_usage"):
        assert p.con.execute(f"SELECT COUNT(*) FROM {t} WHERE page_id LIKE ?", (f"{y}-%",)).fetchone()[0] == 0, t
    without = run(held, "without", [held["scans"] / "x_2030-01-07.pdf", held["scans"] / "o_2030-01-07.pdf"])
    assert dump(p.con, tables=list(BUSINESS)) == dump(without.con, tables=list(BUSINESS))
    rep = build_report(p.con)["intake"]["duplicates"]
    assert rep["pages"] == 4 and rep["sim"]["min"] >= 0.99
    assert p.summary["duplicates"] and {d["page_id"] for d in p.summary["duplicates"]} == {f"{y}-p{n}" for n in py}


def test_pages_of_another_day_are_not_compared(held):
    """같은 손글씨·같은 값의 쪽이라도 다른 날이면 견주지 않는다."""
    split_pages(held["scans"] / "y_2030-01-07.pdf", [1, 2], held["scans"] / "z_2030-01-08.pdf")
    p = run(held)
    z = doc_ids(p.con)["z_2030-01-08"]
    assert {r["status"] for r in pages_of(p.con, z).values()} == {"loaded"}


def test_processing_order_does_not_matter(held):
    """뒤 문서를 먼저 처리하든, 앞 문서를 뒤늦게 다시 처리하든(날짜 바꾸기·되살리기) 처음부터 문서의 순서대로 만든 DB 와 같다.
    앞 문서를 버리거나 다른 날짜로 옮기면 붙잡혀 있던 뒤쪽이 풀려 적재된다."""
    ref = run(held, "ref")
    p = Pipeline(held["st"], site=held["site"])
    for name in ("y_2030-01-07", "o_2030-01-07", "x_2030-01-07"):        # 뒤 문서부터
        p.process_file(held["scans"] / f"{name}.pdf")
    p.process_pending()
    p.finalize()
    assert_same(dump(p.con), dump(ref.con), "뒤 문서부터")
    ids = doc_ids(p.con)
    x, y = ids["x_2030-01-07"], ids["y_2030-01-07"]
    path = held["st"].decisions_path(held["site"].root)

    def decide(items, compare=True):
        decs.save(p.con, path, items, "jp", received=RECEIVED)
        p.process_pending()
        no_null_dates(p.con)
        if compare:                                                   # 처음부터 만든 DB 와 (나머지는 -m fuzz 의 흔들기가 본다)
            fresh = run(held, f"fresh{len(decs.load(path)[0])}")
            assert_same(dump(p.con), dump(fresh.con), items)

    decide([{"target": x, "kind": "date", "value": "2030-01-09"}])          # 앞 문서를 다른 날로 → 뒤쪽이 풀린다
    assert {r["status"] for r in pages_of(p.con, y).values()} == {"loaded"}
    decide([{"target": x, "kind": "date", "value": "2030-01-07"}], compare=False)   # 되돌리면 다시 붙잡힌다
    assert {r["status"] for r in pages_of(p.con, y).values()} == {"duplicate"}
    decide([{"target": x, "kind": "discard"}], compare=False)              # 앞 문서를 버리면 풀린다
    assert {r["status"] for r in pages_of(p.con, y).values()} == {"loaded"}
    decide([{"target": x, "kind": "restore"}])
    assert {r["status"] for r in pages_of(p.con, y).values()} == {"duplicate"}


@pytest.mark.parametrize("choice", ["discard_later", "discard_earlier", "keep_both"])
def test_three_choices_load_the_right_pages(held, choice):
    """같은 종이 — 뒤쪽을 버린다 / 앞쪽을 버리고 뒤쪽을 쓴다 / 다른 종이 — 둘 다 쓴다. 그 뒤 --fresh 로 다시 만들어도 같다."""
    p = run(held)
    ids = doc_ids(p.con)
    x, y = ids["x_2030-01-07"], ids["y_2030-01-07"]
    later, earlier = f"{y}-p2", f"{x}-p2"                          # T01 일보 (JPEG 로 다시 압축한 것)
    items = {"discard_later": [{"target": later, "kind": "discard"}],
             "discard_earlier": [{"target": earlier, "kind": "discard"}],
             "keep_both": [{"target": later, "kind": "keep"}, {"target": earlier, "kind": "keep"}]}[choice]
    path = held["st"].decisions_path(held["site"].root)
    decs.save(p.con, path, items, "jp", received=RECEIVED)
    p.process_pending()
    status = {pid: r[0] for pid, *r in p.con.execute("SELECT page_id, status FROM doc_page WHERE page_id IN (?, ?)",
                                                     (later, earlier))}
    want = {"discard_later": {earlier: "loaded", later: "discarded"},
            "discard_earlier": {earlier: "discarded", later: "loaded"},
            "keep_both": {earlier: "loaded", later: "loaded"}}[choice]
    assert status == want
    n_fields = p.con.execute("SELECT COUNT(*) FROM doc_field WHERE page_id IN (?, ?) AND region = 'haul'",
                             (later, earlier)).fetchone()[0]
    one = p.con.execute("SELECT COUNT(*) FROM doc_field WHERE page_id = ? AND region = 'haul'",
                        (earlier if want[earlier] == "loaded" else later,)).fetchone()[0]
    assert n_fields == one * (2 if choice == "keep_both" else 1)
    assert_same(dump(p.con), dump(run(held, "fresh").con), choice)       # --fresh: 같은 파일·같은 결정으로 처음부터


def test_existing_synthetic_bundles_hold_nothing(null_run, digits_low3, meta_null, usage_run):
    """지금의 합성 묶음(기본, 낮은 칸, 메타 필드, 가동 일보·동시 판·인쇄 층)에서 붙잡힌 쪽이 0 이다."""
    for name, con in (("default", null_run.con), ("low", digits_low3["pipe"].con), ("meta", meta_null["pipe"].con),
                      ("usage", usage_run["pipe"].con)):
        assert con.execute("SELECT COUNT(*) FROM doc_page WHERE status = 'duplicate'").fetchone()[0] == 0, name
        assert con.execute("SELECT COUNT(*) FROM doc_page_sig").fetchone()[0] == \
            con.execute("SELECT COUNT(*) FROM doc_page WHERE status = 'loaded'").fetchone()[0], name
        assert "duplicates" not in (build_report(con).get("intake") or {}), name
