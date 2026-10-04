"""✓ 판정의 정답 (tasks/0004 단계 6): 대기열 checks, 행 하나 = 두 칸의 검수 두 건, eval --checks."""
import json
import threading
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import replace

import pytest

from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.evaluate.checks import evaluate_checks
from minedocscan.pipeline import Pipeline
from minedocscan.review.checks import check_rows
from minedocscan.review.queue import build_queue
from minedocscan.review.server import ReviewApp, make_server
from minedocscan.review.store import load
from minedocscan.tools.synth import T_INSP
from test_review_store import TABLES, _dump

TRUTH_KEY = {True: "yes", False: "no", None: "none"}
NAME = {"yes": "유", "no": "무", "none": "표시 없음", "unknown": "모름"}


@pytest.fixture(scope="module")
def run(synth, tmp_path_factory):
    """3일치 합성(둘째 날은 점검을 하지 않은 날)을 null 로. 검수 파일은 tmp 에."""
    root = tmp_path_factory.mktemp("checks")
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=root / "work", reviews=root / "r" / "reviews.jsonl")
    pipe = Pipeline(settings)
    pipe.run([synth.scans])
    app = ReviewApp(pipe.con, pipe.site, settings, "jp", "checks")
    return {"synth": synth, "settings": settings, "pipe": pipe, "app": app, "root": root}


def _truth(synth) -> dict:
    """(날짜, 행 키) → yes | no | none — 합성 정답(truth.json)에서."""
    return {(r["date"], r["row_key"]): TRUTH_KEY[r["abnormal"]] for d in synth.truth["days"] for r in d["inspection"]}


def _machine_by_hand(con) -> dict:
    """기계의 답을 review/checks.py 와 다른 코드로: 쪽마다 체크 칸이 전부 NULL 이면 표시 없음."""
    rows = con.execute("SELECT f.page_id, f.row_no, f.row_key, f.field_name, f.has_value_raw, p.work_date FROM doc_field f "
                       "JOIN doc_page p ON f.page_id = p.page_id WHERE p.template_name = ? AND f.kind = 'checkmark'",
                       (T_INSP,)).fetchall()
    unused = {p: all(r["has_value_raw"] is None for r in rows if r["page_id"] == p) for p in {r["page_id"] for r in rows}}
    by: dict = {}
    for r in rows:
        by.setdefault((r["work_date"], r["row_key"], r["page_id"]), {})[r["field_name"]] = r["has_value_raw"]
    out = {}
    for (date, key, pid), d in by.items():
        out[(date, key)] = ("유" if d["abnormal_yes"] == 1 else "무" if d["abnormal_no"] == 1
                            else "표시 없음" if unused[pid] else "판정 불가")
    return out


def test_queue_samples_rows_by_date_and_hides_the_machine(run):
    """모집단 = 날짜마다 장비 행 전부 (점검을 하지 않은 날 포함). 응답 어디에도 기계의 판정이 없다."""
    con, site, app = run["pipe"].con, run["pipe"].site, run["app"]
    q = app.queue_json({})
    rows = check_rows(con, site)
    assert q["name"] == "checks" and q["total"] == len(rows) == len(_truth(run["synth"])) and q["done"] == 0
    assert q["show_machine"] is False
    assert {it["work_date"] for it in q["items"]} == {d["date"] for d in run["synth"].truth["days"]}
    for it in q["items"]:
        assert [c["label"] for c in it["cells"]] == ["유", "무"] and it["answer"] is None
        assert all(c["machine"] is None and c["human"] is None and c["review"] is None for c in it["cells"])
    text = json.dumps(q, ensure_ascii=False)
    for bad in ("has_value", "value_raw", "status_raw", "confidence", "backend", "판정 불가", "column_unused", "ink"):
        assert bad not in text, bad
    # 표본: 날짜별로 고르게, 씨앗으로 고정
    small = build_queue(con, "checks", site=site, n=6, seed=3)
    assert Counter(it["work_date"] for it in small["items"]).most_common(1)[0][1] == 2 and small["total"] == 6
    assert small == build_queue(con, "checks", site=site, n=6, seed=3) != build_queue(con, "checks", site=site, n=6, seed=4)
    # 크롭: 행 띠(테두리 없이)와 유·무 두 칸
    fid = q["items"][0]["cells"][0]["field_id"]
    for kind in ("row", "pair"):
        png, _src = app.crop_png({"field_id": fid, "kind": kind, "box": "0"})
        assert png[:4] == b"\x89PNG"


def test_full_truth_review_makes_the_eval_table_of_the_hand_count(run, tmp_path):
    """전 행을 정답대로 검수 → eval --checks 의 표가 손으로 센 것과 같다. 다시 열면 전에 고른 답이 보인다."""
    con, site = run["pipe"].con, run["pipe"].site
    app = ReviewApp(con, site, replace(run["settings"], reviews=tmp_path / "r.jsonl"), "jp", "checks")
    truth = _truth(run["synth"])
    for it in app.queue_json({})["items"]:
        key = (it["work_date"], next(c for c in check_rows(con, site) if c.item_id == it["item_id"]).row_key)
        out = app.post_check({"field_id": it["cells"][1]["field_id"], "answer": truth[key]})      # 무 칸으로 불러도 같은 행
        assert out["answer"] == NAME[truth[key]] and out["applied"]
    machine = _machine_by_hand(con)
    hand = Counter((machine[k], NAME[v]) for k, v in truth.items())
    r = evaluate_checks(con, site)
    got = Counter({(m, t): n for m, row in r["table"].items() for t, n in row.items() if n})
    assert got == hand and r["reviewed_rows"] == len(truth)
    right = sum(n for (m, t), n in hand.items() if m == t)
    decided = sum(n for (m, _t), n in hand.items() if m != "판정 불가")
    assert (r["decided_accuracy"]["k"], r["decided_accuracy"]["n"]) == (right, decided)
    cu = r["column_unused"]
    assert cu["dates"] == len(run["synth"].truth["days"]) and cu["agree"] == cu["dates"]
    assert [p["machine_unused"] for p in cu["by_date"]] == ["inspection_column_unused" in d["scenarios"]
                                                            for d in run["synth"].truth["days"]]
    # 다시 열면: 전부 끝났고 행마다 전에 고른 답
    q = app.queue_json({})
    assert q["done"] == q["total"] == len(truth)
    assert Counter(it["answer"] for it in q["items"]) == Counter(NAME[v] for v in truth.values())
    from minedocscan.review.store import stats

    assert stats(con)["checks"] == {"fields": 2 * len(truth), "rows": len(truth)}
    for split in ("train", "test"):
        assert evaluate_checks(con, site, split)["rows"] <= len(truth)


def test_one_row_is_two_reviews_and_insp_daily_follows_at_once(synth, tmp_path):
    """한 행의 저장 = 두 칸의 검수 두 건. insp_daily 가 바로 갱신되고, 기계 열은 그대로이며, 같은 검수 파일로 새로 돌린 DB 와 같다.
    기계가 유라고 한 행을 무로 고치면 abnormal 은 검수값(0)."""
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=tmp_path / "w1", reviews=tmp_path / "r.jsonl")
    pipe = Pipeline(settings)
    pipe.run([synth.scans])
    con, site = pipe.con, pipe.site
    app = ReviewApp(con, site, settings, "jp", "checks")
    rows = check_rows(con, site)
    flip = next(c for c in rows if c.yes["has_value_raw"] == 1)            # 기계: 유
    before = {f: dict(con.execute("SELECT * FROM doc_field WHERE field_id = ?", (f,)).fetchone())
              for f in (flip.yes["field_id"], flip.no["field_id"])}
    daily = lambda c: con.execute("SELECT d.* FROM insp_daily d JOIN eq_equipment e ON d.equipment_id = e.equipment_id "   # noqa: E731
                                  "WHERE d.inspection_date = ? AND e.equipment_key = ?", (c.work_date, c.row_key)).fetchone()
    assert daily(flip)["abnormal"] == 1
    out = app.post_check({"field_id": flip.yes["field_id"], "answer": "no"})
    recs = [rv for _seq, rv in load(settings.reviews)[0]]
    assert len(recs) == 2 and len(out["review_ids"]) == 2
    assert {(rv.field_id, rv.verdict, rv.value) for rv in recs} == {(flip.yes["field_id"], "empty", ""),
                                                                    (flip.no["field_id"], "value", "1")}
    assert daily(flip)["abnormal"] == 0 and daily(flip)["review_status"] in ("reviewed", "pending")
    for fid, b in before.items():
        a = con.execute("SELECT * FROM doc_field WHERE field_id = ?", (fid,)).fetchone()
        assert (a["has_value_raw"], a["status_raw"], a["value_raw"], a["confidence"]) == \
               (b["has_value_raw"], b["status_raw"], b["value_raw"], b["confidence"])
    assert con.execute("SELECT has_value FROM doc_field WHERE field_id = ?", (flip.no["field_id"],)).fetchone()[0] == 1
    # 판정 불가 → 모름: 두 칸 다 illegible, abnormal 은 기계대로, 행은 pending
    other = next(c for c in rows if c.page_id != flip.page_id and not c.page_unused)
    app.post_check({"field_id": other.no["field_id"], "answer": "unknown"})
    assert daily(other)["review_status"] == "pending"
    unused = next(c for c in rows if c.page_unused)
    app.post_check({"field_id": unused.yes["field_id"], "answer": "none"})
    assert daily(unused)["abnormal"] is None
    # 불변식
    fresh = Pipeline(replace(settings, work_root=tmp_path / "w2"))
    fresh.run([synth.scans])
    assert fresh.summary["reviews"]["imported"] == 6
    for t in TABLES + ("doc_review",):
        assert _dump(fresh.con, t) == _dump(con, t), t


def test_check_api_over_http_and_cli(run, tmp_path, capsys):
    app = ReviewApp(run["pipe"].con, run["pipe"].site, replace(run["settings"], reviews=tmp_path / "r.jsonl"), "jp", "checks")
    httpd = make_server(app, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        rows = check_rows(run["pipe"].con, run["pipe"].site)
        fid = rows[0].yes["field_id"]
        assert post("/api/check", {"field_id": fid, "answer": "maybe"})[0] == 400
        remark = run["pipe"].con.execute("SELECT field_id FROM doc_field WHERE field_name = 'remark'").fetchone()[0]
        assert post("/api/check", {"field_id": remark, "answer": "yes"})[0] == 400
        assert post("/api/check", {"field_id": "nope", "answer": "yes"})[0] == 404
        assert post("/api/review", {"field_id": fid, "verdict": "value", "value": "1"})[0] == 400   # 체크 칸은 /api/check 로만
        status, out = post("/api/check", {"field_id": fid, "answer": "yes"})
        assert status == 200 and out["answer"] == "유"
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            assert "/api/check" in r.read().decode("utf-8")
    finally:
        httpd.shutdown()
        httpd.server_close()
    s = run["settings"]
    capsys.readouterr()
    assert main(["eval", "--checks", "--site", str(s.site), "--work-root", str(s.work_root)]) == 0
    out = capsys.readouterr().out
    assert "✓ 판정" in out and "column_unused" in out
    assert main(["eval", "--checks", "--json", "--site", str(s.site), "--work-root", str(s.work_root)]) == 0
    assert json.loads(capsys.readouterr().out)["checks"]["rows"] == len(rows)
