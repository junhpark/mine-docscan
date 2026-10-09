"""운영 화면 (tasks/0007 단계 5, 4.9): 홈·문서 화면·결정·쪽 그림, 작업 스레드와 화면 스레드, 127.0.0.1 의 검사.

합성 데이터만 — world(conftest: BUNDLES 를 한 번 돌린 DB 의 복사본). 스레드를 쓰는 시험은 하나뿐이고 시간을 재지 않고 신호의 순서로 본다.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import re
import threading
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from conftest import split_pages
from minedocscan.cli import main
from minedocscan.export.daily import daily_book
from minedocscan.export.monthly import monthly_book
from minedocscan.export.xlsx import read_values
from minedocscan.intake import decisions as decs
from minedocscan.intake.worker import Worker
from minedocscan.review import queue as rq
from minedocscan.review.ops import OPS_QUEUES, OpsApp
from minedocscan.review.server import XLSX_TYPE, ApiError, ReviewApp, home_html, make_server
from minedocscan.store.db import open_db, read_txn
from minedocscan.tools.synth import _write_pdf, rotate_scan


@pytest.fixture
def ops(world):
    w = world
    app = OpsApp(w["pipe"].con, w["site"], w["st"], "jp")
    return {**w, "ops": app, "path": w["st"].decisions_path(w["site"].root)}


def undated(o, name="묶음 없는 날짜", src="b_2030-01-07") -> str:
    """날짜 없는 이름의 문서 하나를 등록·처리한다 (needs_date). 돌려주는 값: 문서 ID."""
    f = o["scans"] / f"{name}.pdf"
    split_pages(o["bundles"]["files"][src], [2], f)                     # 바이트가 다른 새 문서 (행렬 한 쪽)
    out = o["pipe"].process_file(f)
    assert out["status"] == "needs_date"
    return out["document_id"]


# ── 홈 ─────────────────────────────────────────────────────────────────────
def test_home_counts_match_the_db_and_do_not_build_sample_queues(ops, monkeypatch):
    con = ops["pipe"].con
    nd = undated(ops)
    for name in ("_haul_numbers", "_checks"):                          # 표본 대기열은 만들지도 않는다
        monkeypatch.setattr(rq, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("표본 대기열")))
    h = ops["ops"].home_json()
    t = h["todo"]
    assert [d["document_id"] for d in t["needs_date"]] == [nd]
    assert {d["page_id"] for d in t["duplicates"]} == {r[0] for r in con.execute(
        "SELECT page_id FROM doc_page WHERE status = 'duplicate'")} and len(t["duplicates"]) == 2
    for name in OPS_QUEUES:
        b = rq.build_queue(con, name, site=ops["site"])
        assert t["queues"][name] == b["total"] - b["done"], name
    pend = t["queues"]["pending"]
    assert sum(pend.values()) == rq.build_queue(con, "pending", site=ops["site"])["total"]
    assert t["unknown_form"] == con.execute("SELECT COUNT(*) FROM doc_page WHERE status = 'unknown_form'").fetchone()[0]
    assert len(h["recent"]) == con.execute("SELECT COUNT(*) FROM doc_document").fetchone()[0]
    assert h["worker"] == {"state": "off"}
    # DB 가 바뀌지 않으면 다시 세지 않는다
    calls = []
    real = rq.build_queue
    monkeypatch.setattr("minedocscan.review.ops.build_queue", lambda *a, **k: calls.append(1) or real(*a, **k))
    ops["ops"].home_json()
    assert calls == []
    con.execute("UPDATE doc_field SET review_status = review_status WHERE rowid = 1")
    con.commit()
    ops["ops"].home_json()
    assert calls


# ── 결정 ───────────────────────────────────────────────────────────────────
def test_date_decision_appends_waits_and_loads_after_a_round(ops):
    """날짜 결정을 보내면 결정 파일에 한 줄이 붙고 문서가 다시 처리 대기가 되며, 작업 한 바퀴 뒤 쪽이 적재되어 있다. 받은 날보다 뒤의
    날짜는 한 번 되묻고(confirm 없이는 저장하지 않는다), 달력에 없는 날짜는 400 이고 파일에 남지 않는다. 여럿 중 하나가 틀리면 아무것도
    남지 않는다."""
    app, con, path = ops["ops"], ops["pipe"].con, ops["path"]
    nd = undated(ops)
    for bad in ([{"target": nd, "kind": "date", "value": "2030-02-30"}],
                [{"target": nd, "kind": "date", "value": "2030-01-07"}, {"target": f"{nd}-p9", "kind": "discard"}],
                [{"target": "nope", "kind": "discard"}]):
        with pytest.raises(ApiError) as e:
            app.post_decision({"items": bad, "confirm": True})
        assert e.value.status == 400
    with pytest.raises(ApiError):
        app.post_decision({"items": []})
    assert not path.exists()
    # 같은 종이(b 의 행렬)가 07일에 있다 — 다른 날로 (07일이면 다시 스캔으로 붙잡힌다)
    r = app.post_decision({"items": [{"target": nd, "kind": "date", "value": "300109"}]})   # 받은 날(오늘)보다 뒤 — 되묻는다
    assert r["ok"] is False and r["confirm"] and not path.exists()
    r = app.post_decision({"items": [{"target": nd, "kind": "date", "value": "300109", "note": "메모"}], "confirm": True})
    assert r["ok"] and r["saved"][0]["value"] == "2030-01-09" and len(path.read_text(encoding="utf-8").splitlines()) == 1
    doc = app.doc_json({"id": nd})["document"]
    assert doc["waiting"] and doc["status"] == "needs_date"
    out = Worker(ops["pipe"]).run_once()
    assert nd not in out.get("needs_date", [])
    assert {x[0] for x in con.execute("SELECT status FROM doc_page WHERE document_id = ?", (nd,))} == {"loaded"}
    d = app.doc_json({"id": nd})
    assert d["document"]["work_date"] == "2030-01-09" and not d["document"]["waiting"]
    assert d["decision"]["date"] == "2030-01-09"


@pytest.mark.parametrize("choice", ["later", "earlier", "keep"])
def test_duplicate_choices_from_the_screen(ops, choice):
    """의심 쪽의 세 선택이 단계 3 과 같은 결과를 낸다 (문서 화면의 단추가 보내는 결정 그대로)."""
    app, con = ops["ops"], ops["pipe"].con
    e = ops["ids"]["e_2030-01-07"]
    view = app.doc_json({"id": e})
    held = [p for p in view["pages"] if p["status"] == "duplicate"]
    assert len(held) == 2 and all(p["earlier"]["page_id"].startswith(ops["ids"]["a_2030-01-07"]) for p in held)
    later, earlier = held[1]["page_id"], held[1]["earlier"]["page_id"]
    items = {"later": [{"target": later, "kind": "discard"}], "earlier": [{"target": earlier, "kind": "discard"}],
             "keep": [{"target": later, "kind": "keep"}, {"target": earlier, "kind": "keep"}]}[choice]
    assert app.post_decision({"items": items})["ok"]
    Worker(ops["pipe"]).run_once()
    st = {pid: s for pid, s in con.execute("SELECT page_id, status FROM doc_page WHERE page_id IN (?, ?)", (later, earlier))}
    assert st == {"later": {earlier: "loaded", later: "discarded"}, "earlier": {earlier: "discarded", later: "loaded"},
                  "keep": {earlier: "loaded", later: "loaded"}}[choice]


def test_document_and_page_discard_and_restore_from_the_screen(ops):
    app, con = ops["ops"], ops["pipe"].con
    b = ops["ids"]["b_2030-01-07"]
    for items, want in (([{"target": b, "kind": "discard"}], "discarded"), ([{"target": b, "kind": "restore"}], "needs_review"),
                        ([{"target": f"{b}-p1", "kind": "discard"}], "needs_review")):
        app.post_decision({"items": items})
        Worker(ops["pipe"]).run_once()
        assert con.execute("SELECT status FROM doc_document WHERE document_id = ?", (b,)).fetchone()[0] in (want, "processed")
    assert con.execute("SELECT status FROM doc_page WHERE page_id = ?", (f"{b}-p1",)).fetchone()[0] == "discarded"
    view = app.doc_json({"id": b})
    assert view["pages"][0]["decision"]["discarded"] and not view["decision"]["discarded"]


# ── 쪽 그림 ─────────────────────────────────────────────────────────────────
def test_page_png_is_upright_and_needs_date_pages_render_from_the_source(ops, monkeypatch):
    """/page.png 는 세운 그림을 준다 (돌아간 합성 쪽에서 바로 선 쪽과 같은 방향). 날짜를 기다리는 문서는 쪽 행이 없어도 원본에서."""
    from minedocscan.imaging.io import load_page

    app = ops["ops"]
    a = ops["ids"]["a_2030-01-07"]
    upright = load_page(ops["bundles"]["files"]["a_2030-01-07"], 2, 200)
    turned = ops["scans"] / "r_2030-01-08.pdf"                         # 다른 날 — 다시 스캔으로 붙잡히지 않게
    _write_pdf(turned, [rotate_scan(upright, 90)])
    r = ops["pipe"].process_file(turned)
    assert r["pages"][0]["rotation"] == 90 and r["pages"][0]["status"] == "loaded"

    def png(**kw):
        return cv2.imdecode(np.frombuffer(app.page_png(kw), np.uint8), cv2.IMREAD_GRAYSCALE)

    want = png(page_id=f"{a}-p2", w=400)
    got = png(page_id=f"{r['document_id']}-p1", w=400)
    assert got.shape == want.shape                                     # 가로 양식 — 돌아간 채면 높이가 다르다
    diff = np.abs(got.astype(int) - want.astype(int)).mean()
    assert diff < 12, diff                                             # JPEG 로 담은 것 — 화소까지는 아니다 (180° 면 훨씬 크다)
    assert diff < 0.5 * np.abs(np.rot90(got, 2).astype(int) - want.astype(int)).mean()
    nd = undated(ops)
    plain = png(doc=nd, page=1, w=300)
    assert plain.shape[1] == 300
    # 화면의 "돌려 보기"(rot)는 서버가 돌린다 — 돌린 그림이 제 칸의 크기를 가진다 (tasks/0008 4.10)
    quarter = png(doc=nd, page=1, w=300, rot=90)
    assert quarter.shape[1] == 300 and abs(quarter.shape[0] / 300 - 300 / plain.shape[0]) < 0.02    # 가로 ↔ 세로
    half = png(doc=nd, page=1, w=300, rot=180)
    assert half.shape == plain.shape and np.abs(half.astype(int) - np.rot90(plain, 2).astype(int)).mean() < 1
    for bad, status in (({"doc": nd, "page": 5}, 404), ({"doc": "zz", "page": 1}, 404), ({"page_id": f"{a}-p1", "w": 5}, 400),
                        ({"doc": nd, "page": 1, "rot": 45}, 400), ({"doc": nd, "page": 1, "rot": "x"}, 400)):
        with pytest.raises(ApiError) as e:
            app.page_png(bad)
        assert e.value.status == status


# ── HTTP ───────────────────────────────────────────────────────────────────
def test_http_routes_checks_and_the_log_has_no_names(ops, capsys):
    """홈·문서 화면·대기열 화면의 길, 다른 출처의 POST 는 403, JSON 이 아닌 본문은 415. 서버 로그에 파일명·날짜·메모가 없다."""
    app = ReviewApp(ops["pipe"].con, ops["site"], ops["st"], "jp", "pending", ops=ops["ops"])
    httpd = make_server(app, port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    nd = undated(ops)

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.getheader("Content-Type") or "", data

    try:
        s, ct, body = req("GET", "/")
        assert s == 200 and b"minedocscan" in body and b"/api/home" in body
        s, ct, body = req("GET", "/?name=pending")
        assert s == 200 and b"/api/queue" in body
        assert req("GET", f"/doc?id={nd}")[2] == req("GET", "/")[2]
        s, _ct, body = req("GET", "/api/home")
        assert s == 200 and json.loads(body)["todo"]["needs_date"][0]["document_id"] == nd
        s, _ct, body = req("GET", "/api/queue?name=pending")
        assert s == 200 and json.loads(body)["home"] is True
        s, ct, body = req("GET", f"/page.png?doc={nd}&page=1&w=200")
        assert s == 200 and ct == "image/png" and body[:4] == b"\x89PNG"
        ok = json.dumps({"items": [{"target": nd, "kind": "date", "value": "2030-01-07", "note": "비밀 메모"}], "confirm": True})
        assert req("POST", "/api/decision", ok, {"Content-Type": "application/json", "Origin": "http://evil.example"})[0] == 403
        assert req("POST", "/api/decision", ok, {"Content-Type": "text/plain"})[0] == 415
        s, _ct, body = req("POST", "/api/decision", ok, {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
        assert s == 200 and json.loads(body)["ok"]
        bad = json.dumps({"items": [{"target": nd, "kind": "date", "value": "2030-13-01"}]})
        assert req("POST", "/api/decision", bad, {"Content-Type": "application/json"})[0] == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
    log = capsys.readouterr().err
    assert "GET /api/home 200" in log and "POST /api/decision 403" in log
    for secret in ("묶음 없는 날짜", "2030-01-07", "비밀 메모", nd):
        assert secret not in log, secret


# ── 스레드 둘 ───────────────────────────────────────────────────────────────
def test_screen_saves_and_crops_while_the_worker_is_in_the_middle_of_a_document(ops):
    """작업 스레드가 문서를 처리하는 중(쪽 하나를 커밋하고 다음 쪽 전)에 화면 스레드의 검수 저장과 크롭 요청이 성공한다 —
    연결 둘, WAL, PyMuPDF 잠금. 순서는 신호로 맞춘다 (시간을 재지 않는다)."""
    pipe = ops["pipe"]
    b, a = ops["ids"]["b_2030-01-07"], ops["ids"]["a_2030-01-07"]
    screen = open_db(ops["st"].resolved_db_url)
    app = ReviewApp(screen, ops["site"], ops["st"], "jp", "pending")
    fid = screen.execute("SELECT field_id FROM doc_field WHERE page_id = ? AND field_name = 'remark' LIMIT 1",
                         (f"{a}-p1",)).fetchone()[0]
    paused, go, seen = threading.Event(), threading.Event(), []
    pipe.on_page = lambda doc, n: (doc == b and n == 1 and not seen) and (seen.append(1), paused.set(), go.wait(60))
    decs.save(screen, ops["path"], [{"target": b, "kind": "restore"}], "jp")
    worker = Worker(pipe)
    errors = []
    t = threading.Thread(target=lambda: errors.append(worker.run_once()), daemon=True)
    t.start()
    try:
        assert paused.wait(60)
        assert worker.status["state"] == "processing" and worker.status["document_id"] == b
        out = app.post_review({"field_id": fid, "verdict": "value", "value": "확인"})
        png, _src = app.crop_png({"field_id": fid, "kind": "cell"})
        assert out["ok"] and png[:4] == b"\x89PNG"
    finally:
        go.set()
        t.join(60)
    assert not t.is_alive() and errors and errors[0]["processed"] >= 1
    assert worker.status == {"state": "idle"}
    assert screen.execute("SELECT value_final FROM doc_field WHERE field_id = ?", (fid,)).fetchone()[0] == "확인"
    screen.close()


def test_an_exception_outside_reading_fails_that_document_and_the_round_goes_on(ops, monkeypatch):
    """작업 스레드의 예외는 그 문서를 failed 로 남기고 다음으로 간다 — 작업이 죽지 않는다."""
    pipe = ops["pipe"]
    b, d = ops["ids"]["b_2030-01-07"], ops["ids"]["d_2030-01-08"]
    decs.save(pipe.con, ops["path"], [{"target": b, "kind": "restore"}, {"target": d, "kind": "restore"}], "jp")
    real = pipe.process_document

    def boom(document_id, *a, **kw):
        if document_id == b:
            raise RuntimeError("DB 가 잠겼다")
        return real(document_id, *a, **kw)

    monkeypatch.setattr(pipe, "process_document", boom)
    out = Worker(pipe).run_once()
    assert b in out["failed"]
    row = pipe.con.execute("SELECT status, error, work_requested, work_done FROM doc_document WHERE document_id = ?",
                           (b,)).fetchone()
    assert row["status"] == "failed" and "RuntimeError" in row["error"] and row["work_done"] == row["work_requested"]
    assert pipe.con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id = ?", (b,)).fetchone()[0] == 0
    assert pipe.pending_documents() == []
    # 바퀴 전체가 실패해도 다음 바퀴를 돈다
    w = Worker(pipe)
    stop = threading.Event()
    calls = {"n": 0}

    def once():
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()
        raise OSError("끊겼다")

    monkeypatch.setattr(w, "run_once", once)
    w.run_forever(stop, 0.0)
    assert calls["n"] == 2 and w.last_error == "OSError"


def test_serve_command_wires_the_worker_and_the_screen(ops, monkeypatch):
    """serve: 작업 스레드(자기 연결)와 화면(자기 연결). serve() 를 바꿔 끼워 띄우지 않고 연결만 본다."""
    import minedocscan.review.server as server

    seen = {}

    def fake(app, host="127.0.0.1", port=8765):
        seen.update(app=app, port=port, running=app.ops.worker is not None)

    monkeypatch.setattr(server, "serve", fake)
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(ops["st"].reviews))
    common = ["--site", str(ops["st"].site), "--archive-root", str(ops["st"].archive_root), "--work-root",
              str(ops["st"].work_root)]
    assert main(["serve", "--reviewer", "jp", "--port", "0", *common]) == 0
    assert seen["running"] and seen["app"].ops.watching and seen["app"].ops.con is not seen["app"].ops.worker.pipe.con
    assert main(["serve", "--reviewer", "jp", "--no-watch", *common]) == 0
    assert seen["app"].ops.worker is None and not seen["app"].ops.watching
    with pytest.raises(SystemExit, match="--reviewer"):
        main(["serve", *common])


# ── 엑셀 내려받기 (tasks/0009 4.1 사·자) ────────────────────────────────────────
@contextlib.contextmanager
def served(app):
    """app 을 포트 0 의 스레드에 띄운다. 돌려주는 값: (포트, req) — req(method, path, body, headers) → (상태, 머리글(소문자), 본문).
    Host 는 127.0.0.1:포트 (headers 로 바꿀 수 있다)."""
    httpd = make_server(app, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        c.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    try:
        yield port, req
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_xlsx_downloads_over_http(ops, tmp_path, capsys):
    """/export/day.xlsx·/export/month.xlsx 를 HTTP 로: 200 이면 xlsx 의 Content-Type, 내려받기 머리글(attachment, 파일 이름 = 날짜·달),
    남기지 않는 응답(no-store), 본문은 모델과 같은 시트를 가진 xlsx(zip). 로컬이 아닌 Host 는 403 이고 파일을 주지 않는다. 달력에 없는
    날짜·꼴이 틀린 날짜·달은 400, 쪽이 없는 날짜·달은 404 (JSON 의 오류). 서버 로그에 날짜가 없다. 검수 화면(ops 없이)에는 이 길이 없다."""
    con, site = ops["pipe"].con, ops["site"]
    with read_txn(con):
        days = [r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date LIKE '2030-01-%' ORDER BY 1")]
        books = {"2030-01-07.xlsx": daily_book(con, site, "2030-01-07"), "2030-01.xlsx": monthly_book(con, site, "2030-01", days)}
    app = ReviewApp(con, site, ops["st"], "jp", "pending", ops=ops["ops"])
    with served(app) as (port, req):
        for path, name in (("/export/day.xlsx?date=2030-01-07", "2030-01-07.xlsx"),
                           ("/export/month.xlsx?month=2030-01", "2030-01.xlsx")):
            s, h, body = req("GET", path)
            assert s == 200 and h["content-type"] == XLSX_TYPE, path
            assert h["content-disposition"] == f'attachment; filename="{name}"' and h["cache-control"] == "no-store"
            assert int(h["content-length"]) == len(body) and body[:2] == b"PK"
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                assert z.testzip() is None and "xl/workbook.xml" in z.namelist()
            (tmp_path / name).write_bytes(body)
            assert list(read_values(tmp_path / name)) == [sh["name"] for sh in books[name]["sheets"]], name
        for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1.evil.example:{port}"):   # DNS 를 바꿔 들어온 요청
            s, h, body = req("GET", "/export/day.xlsx?date=2030-01-07", headers={"Host": host})
            assert s == 403 and "content-disposition" not in h and json.loads(body)["error"], host
        for path, status in (("/export/day.xlsx?date=2030-02-30", 400), ("/export/day.xlsx?date=20300107", 400),
                             ("/export/day.xlsx?date=2030-1-07", 400), ("/export/day.xlsx", 400),
                             ("/export/month.xlsx?month=2030-13", 400), ("/export/month.xlsx?month=2030-1", 400),
                             ("/export/month.xlsx", 400), ("/export/day.xlsx?date=2031-01-07", 404),
                             ("/export/month.xlsx?month=2031-01", 404)):
            s, h, body = req("GET", path)
            assert s == status and h["content-type"].startswith("application/json"), path
            assert "content-disposition" not in h and json.loads(body)["error"], path
    with served(ReviewApp(con, site, ops["st"], "jp", "pending")) as (_port, req):
        s, h, body = req("GET", "/export/day.xlsx?date=2030-01-07")
        assert s == 404 and "content-disposition" not in h
    log = capsys.readouterr().err
    assert "GET /export/day.xlsx 200" in log and "GET /export/month.xlsx 200" in log
    assert "GET /export/day.xlsx 403" in log and "GET /export/month.xlsx 404" in log
    for secret in ("2030-01", "2031", "evil.example"):
        assert secret not in log, secret


def test_xlsx_links_only_for_documents_with_a_page_on_their_date(ops):
    """홈·문서 화면의 has_day: 쪽이 다 쪽의 날짜 결정으로 다른 날짜로 간 문서는 False (그 문서 날짜의 내려받기는 404 다). 쪽 하나라도
    문서 날짜에 남으면 True, 날짜를 기다리는 문서는 False. has_day 인 문서의 날짜는 내려받을 수 있다."""
    app, con = ops["ops"], ops["pipe"].con
    u3, b, a = (ops["ids"][k] for k in ("u3_2030-01-09", "b_2030-01-07", "a_2030-01-07"))
    nd = undated(ops)
    assert app.home_json()["recent"] and all(d["has_day"] is (d["work_date"] is not None) for d in app.home_json()["recent"])
    items = [{"target": f"{u3}-p1", "kind": "date", "value": "2030-01-10"},
             {"target": f"{u3}-p2", "kind": "date", "value": "2030-01-10"},
             {"target": f"{b}-p1", "kind": "date", "value": "2030-01-10"}]
    assert app.post_decision({"items": items, "confirm": True})["ok"]
    Worker(ops["pipe"]).run_once()
    pages = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT page_id, work_date, status FROM doc_page WHERE document_id IN (?, ?)", (u3, b))}
    assert pages == {f"{u3}-p1": ("2030-01-10", "loaded"), f"{u3}-p2": ("2030-01-10", "loaded"),
                     f"{b}-p1": ("2030-01-10", "loaded"), f"{b}-p2": ("2030-01-07", "loaded")}
    home = app.home_json()
    recent = {d["document_id"]: d for d in home["recent"]}
    assert recent[u3]["work_date"] == "2030-01-09" and recent[u3]["has_day"] is False
    assert recent[b]["has_day"] is True and recent[a]["has_day"] is True
    assert recent[nd]["work_date"] is None and recent[nd]["has_day"] is False
    assert [d["has_day"] for d in home["todo"]["needs_date"]] == [False]
    for doc, want in ((u3, False), (b, True), (a, True), (nd, False)):
        assert app.doc_json({"id": doc})["document"]["has_day"] is want, doc
    with pytest.raises(ApiError) as e:                                 # 연결을 두었다면 누르면 404 였다
        app.export_xlsx("day", {"date": "2030-01-09"})
    assert e.value.status == 404
    for day in sorted({d["work_date"] for d in recent.values() if d["has_day"]}):
        data, name = app.export_xlsx("day", {"date": day})
        assert name == f"{day}.xlsx" and data[:2] == b"PK"


def test_home_page_links_and_zoom_keys_in_the_html():
    """home.html (글자로 본다): 엑셀 연결은 has_day 일 때만 만든다 (xlsxLinks 를 부르는 곳마다). 크게 보기를 Esc 로 닫으면 날짜 칸으로
    초점을 옮기되 스크롤하지 않는다 (preventScroll). R 은 Ctrl·Cmd 와 같이 누르면 가로채지 않는다 (새로 고침)."""
    html = home_html().decode("utf-8")
    assert re.search(r"function xlsxLinks\(day\) \{\s*if \(!day\) return '';", html)
    calls = re.findall(r"(?<!function )xlsxLinks\(([^()]*)\)", html)
    assert len(calls) >= 2 and all(re.fullmatch(r"(\w+)\.has_day \? \1\.work_date : null", c) for c in calls), calls
    close = html[html.index("function closeZoom()"):html.index("$('zclose').onclick")]
    assert re.search(r"dateBox\.focus\(\{\s*preventScroll:\s*true\s*\}\)", close) and ".focus()" not in close
    keys = html[html.index("document.onkeydown"):]
    keys = keys[:keys.index("\n    };")]
    assert re.search(r"ev\.key === 'Escape'\)\s*\{[^}]*closeZoom\(\)", keys)
    cond = re.search(r"if \(\(ev\.key === 'r' \|\| ev\.key === 'R'\)([^{]*)\{", keys)
    assert cond and "!ev.ctrlKey" in cond.group(1) and "!ev.metaKey" in cond.group(1), keys
    assert len(re.findall(r"ev\.key === 'r'", html)) == 1                # R 을 받는 곳은 거기 하나


CHROMIUM = "/opt/pw-browsers/chromium"                 # 개발 환경의 Chromium (playwright 의 판과 맞지 않아도 띄울 수 있다)


def test_zoom_keys_in_a_browser(ops):
    """크게 보기를 Chromium 에서 (playwright 와 브라우저가 있을 때만 — 없으면 건너뛴다): 내려 본 자리에서 Esc 로 닫으면 날짜 칸에
    초점이 가고 스크롤이 그대로다. Ctrl·Cmd 와 같이 누른 R 은 가로채지 않고(기본 동작 그대로) 돌리지도 않는다. R 만 누르면 돌린다."""
    sync_api = pytest.importorskip("playwright.sync_api")
    nd = undated(ops)                                    # 날짜를 기다리는 문서 — 방향을 모르는 쪽이라 돌리기(R)가 있다
    app = ReviewApp(ops["pipe"].con, ops["site"], ops["st"], "jp", "pending", ops=ops["ops"])
    with served(app) as (port, _req), sync_api.sync_playwright() as pw:
        browser = None
        for exe in [x for x in (CHROMIUM,) if Path(x).exists()] + [None]:     # 설치된 Chromium, 없으면 playwright 의 것
            try:
                browser = pw.chromium.launch(executable_path=exe)
                break
            except sync_api.Error:
                continue
        if browser is None:
            pytest.skip("Chromium 을 띄울 수 없다")
        try:
            page = browser.new_page(viewport={"width": 900, "height": 500})
            page.goto(f"http://127.0.0.1:{port}/doc?id={nd}")
            page.wait_for_selector("img[data-zoom]")
            zoomed = "!document.getElementById('zoom').hidden"
            page.evaluate("document.body.style.paddingBottom = '4000px'; window.scrollTo(0, 1500)")
            page.evaluate("document.querySelector('img[data-zoom]').click()")       # 스크롤하지 않고 연다
            page.wait_for_function(zoomed)
            y = page.evaluate("window.scrollY")
            assert y >= 1000
            page.keyboard.press("Escape")
            page.wait_for_function("document.getElementById('zoom').hidden")
            assert page.evaluate("document.activeElement.id") == "docdate"
            assert page.evaluate("window.scrollY") == y                             # 날짜 칸(맨 위)으로 뛰지 않았다
            page.evaluate("document.querySelector('img[data-zoom]').click()")
            page.wait_for_function(zoomed)
            press = """(mods) => {
                const ev = new KeyboardEvent('keydown', Object.assign({key: 'r', bubbles: true, cancelable: true}, mods));
                const passed = document.activeElement.dispatchEvent(ev);
                return [passed, document.querySelector('#zbody img').getAttribute('src')];
            }"""
            for mods in ({"ctrlKey": True}, {"metaKey": True}, {"key": "R", "ctrlKey": True, "shiftKey": True}):
                passed, src = page.evaluate(press, mods)
                assert passed and "rot=" not in src, mods                          # 기본 동작을 막지 않았고 돌리지도 않았다
            passed, src = page.evaluate(press, {})
            assert not passed and "rot=90" in src
        finally:
            browser.close()
