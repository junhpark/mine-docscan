"""처리: 날짜 없는 문서, 결정 기록, 지우고 다시 만들기, 쪽마다 커밋, 잠금, 순서 고정 (tasks/0007 단계 2).

전부 합성 데이터 — conftest.BUNDLES 의 2–3쪽짜리 묶음 (운반·점검표는 synth, 가동 일보는 usage_synth 의 쪽). 렌더링·분류·정합은
시험 안에서 저장해 둔 결과를 쓴다 (conftest.fast_imaging — 셋 다 결정적이다). 결정·검수 파일은 tmp_path 에.
불변식 흔들기(결정과 검수를 섞어 넣고 그때마다 처음부터 만든 DB 와 비교)는 test_review_store.py 에 있다 (0001 의 불변식 시험).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from conftest import BUNDLES, fast_imaging
from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.forms.sitepack import SitePack
from minedocscan.intake import decisions as decs
from minedocscan.pipeline import Pipeline
from minedocscan.pipeline.lock import PipelineBusy, PipelineLock
from minedocscan.pipeline.runner import update_document_status
from minedocscan.review.store import Review, field_id_of, save
from minedocscan.store.db import PUBLISH_SKIP_COLUMNS, open_db
from minedocscan.tools.synth import T_INSP, T_LOG

BUSINESS = {"prod_haul": "work_date", "prod_tally": "work_date", "eq_usage_daily": "work_date", "insp_daily": "inspection_date",
            "xcheck_haul": "work_date", "eq_assignment_obs": "work_date", "xcheck_usage": "work_date"}
SKIP = PUBLISH_SKIP_COLUMNS          # 시각·요청 번호·경로 (4.8) — 통합 DB 에 싣지 않는 열과 같다 (store/db.py, tasks/0008 4.8)
RECEIVED = date(2030, 1, 10)                                                      # 결정의 "받은 날" — 시계에 기대지 않는다


def dump(con, skip=SKIP, tables=None, where: dict | None = None) -> dict:
    """테이블마다 정렬한 행 (eq_equipment·meta_schema 빼고 — 장비 마스터는 템플릿에서 온다). where: {테이블: SQL 조건}."""
    names = tables or [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
                       if r[0] not in ("meta_schema", "eq_equipment")]
    out = {}
    for t in names:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})") if r[1] not in skip]
        cond = f" WHERE {where[t]}" if where and t in where else ""
        out[t] = sorted((tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {t}{cond}")), key=repr)
    return out


def assert_same(a: dict, b: dict, what="") -> None:
    assert a.keys() == b.keys()
    for t in a:
        assert a[t] == b[t], (what, t)


def no_null_dates(con) -> None:
    """어느 실행 뒤에도 업무 테이블에 날짜 없는 행이 없다 (4.1)."""
    for t, col in BUSINESS.items():
        assert con.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} IS NULL").fetchone()[0] == 0, t


def copy_bundles(bundles, dest: Path, names, rename: dict | None = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copyfile(bundles["files"][n], dest / f"{(rename or {}).get(n, n)}.pdf")
    return dest


def settings_for(root: Path, site: Path, scans: Path, name: str = "live") -> Settings:
    return Settings(site=site, archive_root=scans, work_root=root / name, reviews=root / "기록" / "reviews.jsonl",
                    save_aligned=False)


def doc_ids(con) -> dict[str, str]:
    return {r["source_name"]: r["document_id"] for r in con.execute("SELECT source_name, document_id FROM doc_document")}


def decide(pipe, items: list[dict]) -> dict:
    return decs.save(pipe.con, pipe.settings.decisions_path(pipe.site.root), items, "jp", received=RECEIVED)


def fresh_of(st: Settings, site: SitePack, scans: Path, root: Path, name: str) -> Pipeline:
    """같은 파일·같은 검수·같은 결정으로 처음부터 (새 WORK_ROOT)."""
    p = Pipeline(replace(st, archive_root=scans, work_root=root / name), site=site)
    p.run([scans])
    return p


# ── 날짜 없는 문서 (4.1·4.2) ───────────────────────────────────────────────
def test_undated_bundle_waits_then_doc_date_equals_dated_run(bundles, tmp_path, monkeypatch, capsys):
    """날짜 규칙에 맞지 않는 이름: needs_date, 쪽·필드·업무 행 0, 종료 코드 0, 요약에 한 줄, doc list 에 쪽 수.
    doc date → watch --once 뒤의 DB = 같은 파일을 날짜 있는 이름으로 처음부터 돌린 것 (둘 다 라벨이 없는 사이트 팩)."""
    fast_imaging(monkeypatch)
    site = tmp_path / "site"
    shutil.copytree(bundles["site"], site, ignore=shutil.ignore_patterns("labels"))   # 라벨은 파일명으로 찾는다 — 없앤다
    names = ["a_2030-01-07", "b_2030-01-07"]
    undated = copy_bundles(bundles, tmp_path / "undated", names, {"a_2030-01-07": "묶음 가", "b_2030-01-07": "scan b"})
    dated = copy_bundles(bundles, tmp_path / "dated", names)
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(tmp_path / "기록" / "reviews.jsonl"))
    common = ["--site", str(site), "--archive-root", str(undated), "--work-root", str(tmp_path / "work")]

    assert main(["run", "--fresh", *common]) == 0
    text = capsys.readouterr()
    assert "날짜를 정할 문서 2건" in text.out
    assert main(["run", "--skip-existing", "--json", *common]) == 0                  # 날짜가 여전히 없다 — 건너뛴다
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["skipped"] == 2 and out["report"]["documents_by_status"] == {"needs_date": 2}
    assert out["report"]["pages"] == 0 and out["report"]["intake"]["needs_date"] == 2
    con = open_db(f"sqlite:///{tmp_path / 'work' / 'minedocscan.db'}")
    for t in ("doc_page", "doc_field", "doc_page_meta", *BUSINESS):
        assert con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    assert main(["doc", "list", "--json", *common]) == 0
    listed = json.loads(capsys.readouterr().out)["documents"]
    assert [(d["source_name"], d["n_pages"], d["status"]) for d in listed] == [("scan b", 2, "needs_date"),
                                                                              ("묶음 가", 2, "needs_date")]
    # 날짜를 정한다 (--reviewer 가 없으면 저장하지 않는다) → watch --once
    target = listed[0]["document_id"]
    with pytest.raises(SystemExit, match="--reviewer"):
        main(["doc", "date", target, "2030-01-07", *common])
    for d in listed:
        assert main(["doc", "date", d["document_id"], "300107" if d is listed[0] else "2030.01.07", "--reviewer", "jp",
                     *common]) == 0
        assert "경고:" in capsys.readouterr().out                                       # 받은 날(오늘)보다 뒤 — 저장은 한다
    assert main(["watch", "--once", "--json", *common]) == 0
    assert json.loads(capsys.readouterr().out)["watch"]["processed"] == 2
    no_null_dates(con)
    assert {r[0] for r in con.execute("SELECT DISTINCT date_source FROM doc_document")} == {"decision"}
    assert [r[0] for r in con.execute("SELECT status FROM doc_page")] == ["loaded"] * 4

    st = Settings(site=site, archive_root=dated, work_root=tmp_path / "work_dated", reviews=tmp_path / "다른" / "reviews.jsonl")
    ref = Pipeline(st)
    ref.run([dated])
    assert {r[0] for r in ref.con.execute("SELECT DISTINCT date_source FROM doc_document")} == {"filename"}
    skip = (*SKIP, "source_name", "source_rel", "date_source", "decided_at")
    meta = {"doc_page_meta": "meta_key <> 'date'"}
    tables = [t for t in dump(ref.con) if t != "doc_decision"]
    assert_same(dump(con, skip, tables, meta), dump(ref.con, skip, tables, meta), "결정으로 정한 날짜 = 파일명의 날짜")
    assert (dump(con, ("source",), ["doc_page_meta"], {"doc_page_meta": "meta_key = 'date'"})
            == dump(ref.con, ("source",), ["doc_page_meta"], {"doc_page_meta": "meta_key = 'date'"}))
    assert {r[0] for r in con.execute("SELECT DISTINCT source FROM doc_page_meta WHERE meta_key = 'date'")} == {"decision"}
    con.close()


def test_iso_only_label_and_page_label_alone_do_not_date_a_document(bundles, tmp_path, monkeypatch):
    """문서의 날짜는 문서의 결정 > 문서 라벨(ISO 날짜만) > 파일명 규칙. 쪽 라벨만 있는 문서도 기다린다 (4.2)."""
    fast_imaging(monkeypatch)
    site = tmp_path / "site"
    shutil.copytree(bundles["site"], site, ignore=shutil.ignore_patterns("labels"))
    (site / "labels").mkdir()
    (site / "labels" / "pages.json").write_text(json.dumps({
        "iso": {"date": "2030-01-08"}, "noniso": {"date": "30.01.08"}, "pageonly#1": {"date": "2030-01-08"}}),
        encoding="utf-8")
    scans = copy_bundles(bundles, tmp_path / "scans", ["d_2030-01-08"], {"d_2030-01-08": "iso"})
    for n in ("noniso", "pageonly"):
        shutil.copyfile(scans / "iso.pdf", scans / f"{n}.pdf")
    st = settings_for(tmp_path, site, scans)
    out = {}
    for n in ("iso", "noniso", "pageonly"):                    # 같은 바이트 = 같은 문서 ID — 하나씩 따로 돈다
        p = Pipeline(replace(st, work_root=tmp_path / f"w_{n}"))
        out[n] = p.process_file(scans / f"{n}.pdf")["status"]
        p.con.close()
    assert out == {"iso": "ok", "noniso": "needs_date", "pageonly": "needs_date"}


# ── 결정: 날짜 바꾸기, 버리기·되살리기 (4.3·4.8) ──────────────────────────
def test_date_change_recomputes_old_date_and_new_date(world):
    """날짜를 바꾸면 옛 날짜의 교차검증·배차 관측·계기의 연속성 = 그 문서 없이 계산한 것, 쪽이 하나도 안 남은 날짜의 행은 사라진다.
    새 날짜에는 그 문서가 들어 있다."""
    pipe, ids, root = world["pipe"], world["ids"], world["root"]
    con = pipe.con
    assert con.execute("SELECT COUNT(*) FROM xcheck_haul WHERE work_date = '2030-01-08'").fetchone()[0] > 0
    # d 는 2030-01-08 의 유일한 운반 문서, u2 는 그날의 유일한 가동 일보
    decide(pipe, [{"target": ids["d_2030-01-08"], "kind": "date", "value": "2030-01-11"},
                  {"target": ids["u2_2030-01-08"], "kind": "date", "value": "2030-01-11"},
                  {"target": ids["b_2030-01-07"], "kind": "date", "value": "2030-01-09"}])
    assert sorted(pipe.pending_documents()) == sorted(ids[n] for n in ("d_2030-01-08", "u2_2030-01-08", "b_2030-01-07"))
    pipe.process_pending()
    no_null_dates(con)
    for t in ("xcheck_haul", "eq_assignment_obs", "prod_haul", "eq_usage_daily", "xcheck_usage"):
        assert con.execute(f"SELECT COUNT(*) FROM {t} WHERE work_date = '2030-01-08'").fetchone()[0] == 0, t
    assert con.execute("SELECT COUNT(*) FROM xcheck_haul WHERE work_date = '2030-01-11'").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM xcheck_haul WHERE work_date = '2030-01-09'").fetchone()[0] > 0
    # 옛 날짜(01-07)·남은 날짜(01-09 의 가동 일보)는 옮긴 문서 없이 돌린 것과 같다
    rest = [n for n in BUNDLES if n not in ("d_2030-01-08", "u2_2030-01-08", "b_2030-01-07")]
    without = fresh_of(world["st"], world["site"], copy_bundles(world["bundles"], root / "without", rest), root, "w_without")
    old = {t: f"{c} IN ('2030-01-07')" for t, c in BUSINESS.items()}
    assert_same(dump(con, tables=list(BUSINESS), where=old), dump(without.con, tables=list(BUSINESS), where=old), "옛 날짜")
    cont = {"xcheck_usage": "work_date IN ('2030-01-07', '2030-01-09')"}
    assert dump(con, tables=["xcheck_usage"], where=cont) == dump(without.con, tables=["xcheck_usage"], where=cont)
    # 결정까지 같은 처음부터 = 지금 — 불변식 흔들기(test_review_store)가 본다


def test_discard_and_restore_document_and_page(world):
    """버린 문서의 행은 0 이고 나머지는 그 문서 없이 돌린 것과 같다. 되살리면 원래와 같다. 쪽에 건 버리기·날짜도 같다."""
    pipe, ids, root = world["pipe"], world["ids"], world["root"]
    con = pipe.con
    original = dump(con)
    world["original_meta"] = dump(con, (*SKIP, "source"), ["doc_page_meta"])
    b = ids["b_2030-01-07"]
    decide(pipe, [{"target": b, "kind": "discard"}])
    pipe.process_pending()
    row = con.execute("SELECT * FROM doc_document WHERE document_id = ?", (b,)).fetchone()
    assert row["status"] == "discarded" and row["n_pages"] == 2
    assert con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id = ?", (b,)).fetchone()[0] == 0
    rest = [n for n in BUNDLES if n != "b_2030-01-07"]
    without = fresh_of(world["st"], world["site"], copy_bundles(world["bundles"], root / "without", rest), root, "w_without")
    other = {"doc_document": f"document_id <> '{b}'", "doc_decision": "0"}
    assert_same(dump(con, where=other), dump(without.con, where=other), "버린 문서 = 없는 문서")
    decide(pipe, [{"target": b, "kind": "restore"}])
    pipe.process_pending()
    assert_same(dump(con, tables=[t for t in original if t != "doc_decision"]),
                {t: v for t, v in original.items() if t != "doc_decision"}, "되살리면 원래대로")
    # 쪽: a 의 둘째 쪽(T01 일보)을 버리고, c 의 둘째 쪽(T01 일보)은 다른 날짜로
    a, c = ids["a_2030-01-07"], ids["c_2030-01-07"]
    decide(pipe, [{"target": f"{a}-p2", "kind": "discard"}, {"target": f"{c}-p2", "kind": "date", "value": "2030-01-12"}])
    pipe.process_pending()
    pg = {r["page_id"]: r for r in con.execute("SELECT * FROM doc_page WHERE document_id IN (?, ?)", (a, c))}
    assert pg[f"{a}-p2"]["status"] == "discarded" and pg[f"{a}-p1"]["status"] == "loaded"
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE page_id = ?", (f"{a}-p2",)).fetchone()[0] == 0
    assert pg[f"{c}-p2"]["work_date"] == "2030-01-12" and pg[f"{c}-p1"]["work_date"] == "2030-01-07"
    assert {r[0] for r in con.execute("SELECT DISTINCT work_date FROM prod_haul WHERE page_id = ?", (f"{c}-p2",))} == {"2030-01-12"}
    assert con.execute("SELECT status FROM doc_document WHERE document_id = ?", (a,)).fetchone()[0] in ("processed", "needs_review")
    no_null_dates(con)
    assert_same(dump(con), dump(fresh_of(world["st"], world["site"], world["scans"], root, "w_fresh").con), "쪽의 결정")
    decide(pipe, [{"target": f"{a}-p2", "kind": "restore"}, {"target": f"{c}-p2", "kind": "date", "value": "2030-01-07"}])
    pipe.process_pending()
    back = dump(con)                                              # 날짜는 같고 출처만 decision (결정으로 되돌렸다)
    assert back["doc_page_meta"] != original["doc_page_meta"]
    assert_same({t: v for t, v in back.items() if t not in ("doc_decision", "doc_page_meta")},
                {t: v for t, v in original.items() if t not in ("doc_decision", "doc_page_meta")}, "쪽을 되살리고 날짜를 되돌리면")
    no_source = (*SKIP, "source")
    assert dump(con, no_source, ["doc_page_meta"]) == world["original_meta"]


def test_meta_review_keeps_the_decided_date(bundles, tmp_path, monkeypatch):
    """차량번호(메타 필드)를 검수한 뒤에도 결정으로 정한 날짜가 그 쪽의 doc_page_meta 와 업무 행에 남아 있다 (refresh_page)."""
    fast_imaging(monkeypatch)
    scans = copy_bundles(bundles, tmp_path / "scans", ["d_2030-01-08"], {"d_2030-01-08": "no date"})
    st = settings_for(tmp_path, bundles["site"], scans)
    pipe = Pipeline(st)
    pipe.run([scans])
    doc = doc_ids(pipe.con)["no date"]
    decide(pipe, [{"target": doc, "kind": "date", "value": "2030-01-08"}])
    pipe.process_pending()
    page = pipe.con.execute("SELECT page_id FROM doc_page WHERE document_id = ? AND template_name = ?", (doc, T_LOG)).fetchone()[0]
    save(pipe.con, pipe.site, st, Review(field_id_of(page, "vehicle_no"), "value", "V-104", "jp"))
    m = {r["meta_key"]: r for r in pipe.con.execute("SELECT * FROM doc_page_meta WHERE page_id = ?", (page,))}
    assert (m["date"]["value"], m["date"]["source"]) == ("2030-01-08", "decision")
    assert (m["vehicle_no"]["value"], m["vehicle_no"]["source"]) == ("V-104", "review")
    assert {r[0] for r in pipe.con.execute("SELECT DISTINCT work_date FROM prod_haul WHERE page_id = ?", (page,))} == {"2030-01-08"}
    no_null_dates(pipe.con)


# ── 요청 번호, 상태 (4.1) ──────────────────────────────────────────────────
def test_request_during_processing_survives_and_review_keeps_status(world):
    """처리하는 도중에 온 결정은 사라지지 않는다 (요청 번호). 처리 도중(received)에 저장한 검수는 상태를 바꾸지 않는다."""
    pipe, ids = world["pipe"], world["ids"]
    a = ids["a_2030-01-07"]
    other = open_db(world["st"].resolved_db_url)                 # 다른 연결 (화면·명령)
    seen = []

    def on_page(document_id, page_no):
        if document_id != a or page_no != 1 or seen:
            return
        seen.append(other.execute("SELECT status FROM doc_document WHERE document_id = ?", (a,)).fetchone()[0])
        decs.save(other, world["st"].decisions_path(pipe.site.root), [{"target": f"{a}-p2", "kind": "discard"}], "jp",
                  received=RECEIVED)
        fid = other.execute("SELECT field_id FROM doc_field WHERE page_id = ? AND field_name = 'remark' LIMIT 1",
                            (f"{a}-p1",)).fetchone()[0]
        save(other, pipe.site, world["st"], Review(fid, "value", "ok", "jp"))
        seen.append(other.execute("SELECT status FROM doc_document WHERE document_id = ?", (a,)).fetchone()[0])

    pipe.on_page = on_page
    decide(pipe, [{"target": a, "kind": "restore"}])             # 버린 적은 없다 — 다시 처리만 요청한다
    out = pipe.process_document(a)
    assert out["status"] == "ok" and seen == ["received", "received"]
    assert pipe.pending_documents()[0] == a                       # 도중에 온 요청이 남았다 (뒤 문서 e — a 의 다시 스캔 — 도 대기)
    from minedocscan.report import build_report

    assert build_report(pipe.con)["intake"]["waiting"] == len(pipe.pending_documents())
    assert pipe.con.execute("SELECT status FROM doc_page WHERE page_id = ?", (f"{a}-p2",)).fetchone()[0] == "loaded"
    pipe.process_pending()
    assert pipe.pending_documents() == []
    assert pipe.con.execute("SELECT status FROM doc_page WHERE page_id = ?", (f"{a}-p2",)).fetchone()[0] == "discarded"
    other.close()


def test_update_document_status_moves_only_between_processed_and_needs_review(world):
    con, a = world["pipe"].con, world["ids"]["a_2030-01-07"]
    for status in ("received", "needs_date", "discarded", "failed"):
        con.execute("UPDATE doc_document SET status = ? WHERE document_id = ?", (status, a))
        assert update_document_status(con, a) == status
        assert con.execute("SELECT status FROM doc_document WHERE document_id = ?", (a,)).fetchone()[0] == status
    con.execute("UPDATE doc_document SET status = 'processed' WHERE document_id = ?", (a,))
    assert update_document_status(con, a) == "needs_review"          # 검수 대기 칸이 있다


# ── 결정 파일 (4.3) ────────────────────────────────────────────────────────
def test_decision_file_is_append_only_rejects_bad_input_and_survives_fresh(bundles, tmp_path, monkeypatch, capsys):
    fast_imaging(monkeypatch)
    scans = copy_bundles(bundles, tmp_path / "scans", ["a_2030-01-07", "d_2030-01-08"])
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(tmp_path / "기록" / "reviews.jsonl"))
    common = ["--site", str(bundles["site"]), "--archive-root", str(scans), "--work-root", str(tmp_path / "work")]
    assert main(["run", "--fresh", *common]) == 0
    capsys.readouterr()
    st = Settings(site=bundles["site"], reviews=tmp_path / "기록" / "reviews.jsonl")
    path = st.decisions_path(bundles["site"])
    assert path == tmp_path / "기록" / "decisions.jsonl"
    con = open_db(f"sqlite:///{tmp_path / 'work' / 'minedocscan.db'}")
    ids = doc_ids(con)
    a, d = ids["a_2030-01-07"], ids["d_2030-01-08"]
    assert main(["doc", "date", a, "2030-01-09", "--reviewer", "jp", *common]) == 0
    first = path.read_bytes()
    for bad, why in (([a, "2030-02-30"], "달력"), ([a, "0230"], "달력"), (["ffffffffffffffff", "2030-01-09"], "모르는 문서"),
                     ([f"{a}-p9", "2030-01-09"], "9쪽이 없습니다")):
        with pytest.raises(SystemExit, match=why):
            main(["doc", "date", *bad, "--reviewer", "jp", *common])
    with pytest.raises(SystemExit, match="쪽에만"):
        main(["doc", "keep", a, "--reviewer", "jp", *common])
    with pytest.raises(decs.DecisionError):                       # 여럿 중 하나라도 틀리면 아무것도 남기지 않는다
        decs.save(con, path, [{"target": d, "kind": "discard"}, {"target": f"{d}-p3", "kind": "discard"}], "jp")
    assert path.read_bytes() == first
    assert con.execute("SELECT work_requested FROM doc_document WHERE document_id = ?", (d,)).fetchone()[0] == 0
    assert main(["doc", "discard", f"{d}-p2", "--reviewer", "jp", "--note", "메모", *common]) == 0
    assert "메모" not in capsys.readouterr().out                    # 메모는 출력하지 않는다
    assert path.read_bytes().startswith(first) and len(path.read_bytes().splitlines()) == 2
    # --fresh: DB 를 지우고 다시 만들어도 같은 파일이면 결정이 그대로 붙는다
    assert main(["run", "--fresh", "--json", *common]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["decisions"]["imported"] == 2
    con = open_db(f"sqlite:///{tmp_path / 'work' / 'minedocscan.db'}")
    assert con.execute("SELECT work_date, date_source FROM doc_document WHERE document_id = ?", (a,)).fetchone()[:] == (
        "2030-01-09", "decision")
    assert con.execute("SELECT status FROM doc_page WHERE page_id = ?", (f"{d}-p2",)).fetchone()[0] == "discarded"
    assert main(["info", "--json", *common]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["decisions"] == {"path": str(path), "lines": 2}
    rep = out["report"]["intake"]
    assert rep["discarded"] == {"documents": 0, "pages": 1} and rep["date_source"] == {"decision": 1, "filename": 1}
    con.close()


# ── 쪽마다 커밋, 실패, 끊김 (4.8) ──────────────────────────────────────────
def test_pages_are_committed_one_by_one(world):
    """문서를 처리하는 도중에 다른 연결에서 앞쪽의 행이 보인다 (쪽이 끝날 때의 훅 — 시간을 재지 않는다)."""
    pipe, b = world["pipe"], world["ids"]["b_2030-01-07"]
    other = open_db(world["st"].resolved_db_url)
    seen = []
    pipe.on_page = lambda doc, n: doc == b and seen.append((n, other.execute(
        "SELECT COUNT(*) FROM doc_page WHERE document_id = ?", (doc,)).fetchone()[0], other.execute(
        "SELECT COUNT(*) FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id WHERE p.document_id = ?",
        (doc,)).fetchone()[0] > 0))
    decide(pipe, [{"target": b, "kind": "restore"}])
    pipe.process_pending()
    assert seen == [(1, 1, True), (2, 2, True)]
    other.close()


def test_read_failure_leaves_no_rows_and_interruption_resumes(world, monkeypatch):
    """문서를 읽다 실패하면 그 문서의 행이 0 이고 failed. 처리 도중 끊고(received 인 채로) 다시 처리하면 한 번에 돌린 것과 같다."""
    import minedocscan.pipeline.runner as runner

    pipe, ids, root = world["pipe"], world["ids"], world["root"]
    con, c = pipe.con, ids["c_2030-01-07"]
    load = runner.load_pages

    def broken(path, *a, **kw):
        for n, g in load(path, *a, **kw):
            if n == 2 and Path(path).name.startswith("c_"):
                raise OSError("읽다 끊겼습니다")
            yield n, g

    monkeypatch.setattr(runner, "load_pages", broken)
    decide(pipe, [{"target": c, "kind": "restore"}])
    assert pipe.process_pending() == 2                             # c, 그리고 같은 날·계열에 붙잡힌 쪽이 있는 뒤 문서 e (4.6 ①)
    row = con.execute("SELECT * FROM doc_document WHERE document_id = ?", (c,)).fetchone()
    assert row["status"] == "failed" and "읽다 끊겼습니다" in row["error"] and row["work_done"] == row["work_requested"]
    assert con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id = ?", (c,)).fetchone()[0] == 0
    rest = [n for n in BUNDLES if n != "c_2030-01-07"]
    without = fresh_of(world["st"], world["site"], copy_bundles(world["bundles"], root / "without", rest), root, "w_without")
    other = {"doc_document": f"document_id <> '{c}'", "doc_decision": "0"}
    assert_same(dump(con, where=other), dump(without.con, where=other), "실패한 문서 = 없는 문서")
    monkeypatch.setattr(runner, "load_pages", load)

    # 끊김: 첫 쪽을 커밋한 뒤 프로세스가 죽는다 (KeyboardInterrupt — 문서의 실패로 잡지 않는다)
    def die(doc, n):
        raise KeyboardInterrupt

    pipe.on_page = die
    decide(pipe, [{"target": c, "kind": "restore"}])
    with pytest.raises(KeyboardInterrupt):
        pipe.process_pending()
    assert con.execute("SELECT status FROM doc_document WHERE document_id = ?", (c,)).fetchone()[0] == "received"
    assert con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id = ?", (c,)).fetchone()[0] == 1   # 만들다 만 것
    again = Pipeline(world["st"], site=world["site"])             # 다음 실행 (새 프로세스처럼)
    assert again.pending_documents() == [c]
    assert again.process_pending() == 2
    no_null_dates(again.con)
    assert_same(dump(again.con), dump(fresh_of(world["st"], world["site"], world["scans"], root, "w_fresh").con), "끊긴 뒤")


def test_unreachable_source_is_left_alone(world):
    """원본에 닿지 않으면 그 문서를 건드리지 않고 다음에 다시 본다 (요약에 알린다)."""
    pipe, ids = world["pipe"], world["ids"]
    d = ids["d_2030-01-08"]
    before = dump(pipe.con)
    moved = world["root"] / "moved"
    moved.mkdir()
    shutil.move(str(world["scans"] / "d_2030-01-08.pdf"), moved / "d_2030-01-08.pdf")
    decide(pipe, [{"target": d, "kind": "date", "value": "2030-01-09"}])
    assert pipe.process_pending() == 0 and pipe.summary["unreachable"] == [d]
    after = dump(pipe.con)
    assert {t: v for t, v in after.items() if t != "doc_decision"} == {t: v for t, v in before.items() if t != "doc_decision"}
    assert pipe.pending_documents() == [d]
    shutil.move(str(moved / "d_2030-01-08.pdf"), world["scans"] / "d_2030-01-08.pdf")
    assert pipe.process_pending() == 1 and pipe.pending_documents() == []


# ── 잠금 (4.1) ─────────────────────────────────────────────────────────────
def test_second_pipeline_process_is_refused_until_the_first_dies(tmp_path, bundles):
    """파이프라인을 도는 둘째 프로세스는 시작하지 않고 한 줄로 알린다. 첫째가 죽은 뒤에는 시작한다 (잠들지 않는다 — 줄을 읽는다)."""
    work = tmp_path / "work"
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys
        from minedocscan.pipeline.lock import PipelineLock
        PipelineLock({str(work)!r}).acquire()
        print("ready", flush=True)
        sys.stdin.read()
        """)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(PipelineBusy):
            PipelineLock(work).acquire()
        common = ["--site", str(bundles["site"]), "--archive-root", str(tmp_path), "--work-root", str(work)]
        for cmd in (["run"], ["watch", "--once"]):
            with pytest.raises(SystemExit) as e:
                main([*cmd, *common])
            assert "\n" not in str(e.value) and "돌고 있" in str(e.value)
    finally:
        holder.kill()                                             # 죽는다 — 운영체제가 파일 잠금을 푼다
        holder.wait()
    lock = PipelineLock(work).acquire()
    lock.release()


# ── 순서 고정 (4.8) ────────────────────────────────────────────────────────
def test_slot_collision_and_inspection_follow_page_order_not_insert_order(world):
    """2030-01-07 에 T01 일보가 두 쪽(a·c), 점검표가 두 쪽(a·c). 자리는 쪽의 순서가 앞인 쪽(a)이, 점검 행은 뒤인 쪽(c)이 갖는다.
    a 를 다시 처리해 행이 맨 뒤로 가도 그대로다. 검수는 이기는 쪽의 칸일 때만 점검 행을 고친다."""
    pipe, ids = world["pipe"], world["ids"]
    con, a, c = pipe.con, ids["a_2030-01-07"], ids["c_2030-01-07"]

    def state():
        slots = {r["page_id"]: r["slot"] for r in con.execute(
            "SELECT DISTINCT page_id, slot FROM prod_haul WHERE work_date = '2030-01-07' AND source_role = 'log'")}
        insp = {r["page_id"] for r in con.execute("SELECT page_id FROM insp_daily WHERE inspection_date = '2030-01-07'")}
        return slots, insp, dump(con, tables=list(BUSINESS))

    slots, insp, rows = state()
    assert slots[f"{a}-p2"] == "T01" and slots[f"{c}-p2"] is None and insp == {f"{c}-p1"}
    decide(pipe, [{"target": a, "kind": "restore"}])
    pipe.process_pending()
    assert state() == (slots, insp, rows)
    eid = con.execute("SELECT inspection_id, remark FROM insp_daily WHERE page_id = ? LIMIT 1", (f"{c}-p1",)).fetchone()
    key = con.execute("SELECT equipment_key FROM eq_equipment WHERE equipment_id = ?", (eid[0].split(":", 1)[1],)).fetchone()[0]

    def remark_of(page):
        return con.execute("SELECT field_id FROM doc_field WHERE page_id = ? AND field_name = 'remark' AND row_key = ?",
                           (page, key)).fetchone()[0]

    save(con, pipe.site, world["st"], Review(remark_of(f"{a}-p1"), "value", "지는 쪽", "jp"))
    assert con.execute("SELECT remark FROM insp_daily WHERE inspection_id = ?", (eid[0],)).fetchone()[0] == eid[1]
    save(con, pipe.site, world["st"], Review(remark_of(f"{c}-p1"), "value", "이기는 쪽", "jp"))
    assert con.execute("SELECT remark FROM insp_daily WHERE inspection_id = ?", (eid[0],)).fetchone()[0] == "이기는 쪽"
    # c 를 버리면 a 의 점검표가 그 날짜의 점검 행이 된다
    decide(pipe, [{"target": c, "kind": "discard"}])
    pipe.process_pending()
    assert {r[0] for r in con.execute("SELECT page_id FROM insp_daily WHERE inspection_date = '2030-01-07'")} == {f"{a}-p1"}
    assert con.execute("SELECT remark FROM insp_daily WHERE inspection_id = ?", (eid[0],)).fetchone()[0] == "지는 쪽"
    assert con.execute("SELECT COUNT(*) FROM doc_page WHERE template_name = ? AND status = 'loaded'", (T_INSP,)).fetchone()[0] == 1
    assert_same(dump(con), dump(fresh_of(world["st"], world["site"], world["scans"], world["root"], "w_fresh2").con))


def test_crosscheck_counts_unchanged_by_the_fixed_order(null_run, synth):
    """교차검증의 순서를 고정한 뒤에도 합성 묶음의 교차검증 수가 그대로다 (지금의 run 이 그 순서로 넣는다)."""
    from minedocscan.report import build_report
    from minedocscan.tools.synth import expected_xcheck

    assert build_report(null_run.con)["xcheck_haul"] == expected_xcheck(synth.truth["days"], with_trips=False)


def test_usage_order_breaks_ties_by_page_id():
    """같은 장비의 기록이 날짜·시작 값·문서 이름·쪽 번호까지 같으면 쪽 ID 가 가른다 — 넣은 순서에 기대지 않는다."""
    from minedocscan.validate.usage import _order, continuity_rows

    def rec(pid, start, end):
        return {"page_id": pid, "work_date": "2030-01-07", "meter_start": start, "meter_end": end, "source_name": "scan",
                "page_no": 1, "ref": "id:EQ-1", "reading_kind": "meter", "start_field_id": f"{pid}:s", "end_field_id": f"{pid}:e"}

    rs = [rec("b-p1", 10.0, 12.0), rec("a-p1", 10.0, 11.0), rec("c-p1", 12.0, 13.0)]
    one = continuity_rows(sorted(rs, key=_order))
    two = continuity_rows(sorted(reversed(rs), key=_order))
    assert [r["page_id"] for r in sorted(rs, key=_order)] == ["a-p1", "b-p1", "c-p1"]
    assert one == two
