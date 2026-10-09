"""자동 내보내기와 화면 (tasks/0008 단계 4, 4.7): 처리·검수가 건드린 날짜를 넉넉하게, 바퀴 끝의 내보내기, 전체 훑기(시계 주입),
쓰지 못한 파일·없어진 폴더, 홈의 상태, 내려받기.

합성 데이터만 — world(conftest: BUNDLES 를 한 번 돌린 DB 의 복사본: 운반·점검표 문서 넷, 운행일보 TRUCK 이 사흘). 스레드를 쓰는 시험은
하나뿐이고 신호의 순서로 본다 (시간을 재지 않는다). 접수 묶음을 끝까지 도는 것은 -m slow.
"""
from __future__ import annotations

import shutil
import threading
from dataclasses import replace

import pytest

from minedocscan.export.auto import AutoExport
from minedocscan.export.daily import daily_book
from minedocscan.export.monthly import monthly_book
from minedocscan.export.writer import DAILY_RE, MONTHLY_RE, daily_path, export_excel, monthly_path
from minedocscan.export.xlsx import read_values
from minedocscan.intake import decisions as decs
from minedocscan.intake.worker import Worker, format_round
from minedocscan.review.ops import OpsApp
from minedocscan.review.server import ApiError
from minedocscan.review.store import review_from_field, save
from minedocscan.store.db import open_db, read_txn
from minedocscan.touched import Touched


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def auto(world, out, sweep=0.0, clock=None) -> AutoExport:
    st = replace(world["st"], excel_dir=out, export_sweep_minutes=sweep)
    return AutoExport(st, world["site"], clock=clock or Clock(), now=lambda: "2030-02-01T00:00:00Z")


def model_rows(sheet: dict) -> list[list]:
    rows = []
    for row in sheet["rows"]:
        vals = [None if c[1].startswith("stamp") else c[0] for c in row]
        while vals and vals[-1] is None:
            vals.pop()
        rows.append(vals)
    while rows and not rows[-1]:
        rows.pop()
    return rows


def same_as_db(con, site, out) -> int:
    """엑셀 폴더의 파일 전부가 지금의 DB 의 모델과 같다 (되읽어서 — 만든 시각·판 칸은 빼고). 쪽이 있는 날짜·달마다 파일이 있다.
    돌려주는 값: 견준 파일 수."""
    days = sorted(r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date IS NOT NULL"))
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.xlsx"))
    assert files == sorted([daily_path(d) for d in days] + [monthly_path(m) for m in {d[:7] for d in days}])
    for rel in files:
        with read_txn(con):
            if m := DAILY_RE.match(rel):
                book = daily_book(con, site, m.group(2))
            else:
                m = MONTHLY_RE.match(rel)
                book = monthly_book(con, site, m.group(1), [d for d in days if d.startswith(m.group(1))])
        got = read_values(out / rel)
        assert list(got) == [s["name"] for s in book["sheets"]], rel
        for s in book["sheets"]:
            want, have = model_rows(s), got[s["name"]]
            for i, row in enumerate(s["rows"]):
                if any(c[1].startswith("stamp") for c in row):
                    assert have[i][1]
                    have[i], want[i] = have[i][:1], want[i][:1]
            assert have == want, (rel, s["name"])
    return len(files)


def nothing_missed(world, out) -> None:
    """끝난 뒤 전체를 훑으면 쓸 파일이 0 이다 (놓친 날짜가 없다)."""
    r = export_excel(world["pipe"].con, world["site"], out, full=True)
    assert (r.written, r.deleted, r.failed) == ([], [], []), r


def usage_pages(con, template_like: str = "synth_usage_log%") -> list[dict]:
    """운행일보 TRUCK 쪽 (날짜 순) — u1·u2·u3 의 첫 쪽."""
    return [dict(r) for r in con.execute(
        "SELECT p.page_id, p.work_date, p.document_id FROM doc_page p WHERE p.template_name LIKE ? AND p.page_no = 1 "
        "AND p.status = 'loaded' ORDER BY p.work_date", (template_like,))]


def meter_fields(con, page_id: str) -> dict[str, str]:
    u = con.execute("SELECT start_field_id, end_field_id FROM eq_usage_daily WHERE page_id = ?", (page_id,)).fetchone()
    return {"start": u["start_field_id"], "end": u["end_field_id"]}


def chain(world, st) -> list[dict]:
    """TRUCK 사흘의 장비명과 계기(시작·종료)를 검수로 정한다 — 계기가 이어진다 (100→110→120→130). 건드린 것을 넘기지 않는다."""
    con, site = world["pipe"].con, world["site"]
    pages = usage_pages(con)
    assert [p["work_date"] for p in pages] == ["2030-01-07", "2030-01-08", "2030-01-09"]
    for k, p in enumerate(pages):
        save(con, site, st, review_from_field(con, f"{p['page_id']}:fields:equipment:-1", "value", "TRUCK", "jp"))
        m = meter_fields(con, p["page_id"])
        save(con, site, st, review_from_field(con, m["start"], "value", f"{100 + 10 * k}.0", "jp"))
        save(con, site, st, review_from_field(con, m["end"], "value", f"{110 + 10 * k}.0", "jp"))
    res = {r["page_id"]: r["result"] for r in con.execute("SELECT page_id, result FROM xcheck_usage WHERE check_kind = 'continuity'")}
    assert [res.get(p["page_id"]) for p in pages[1:]] == ["match", "match"], res
    return pages


# ── 넉넉한 더러운 날짜 (전체 훑기를 끄고 — 시작할 때의 훑기 뒤에) ─────────────────────────
def test_reviews_spread_to_the_days_they_change(world, tmp_path):
    con, site = world["pipe"].con, world["site"]
    st = world["st"]
    pages = chain(world, st)
    out = tmp_path / "엑셀"
    out.mkdir()
    x = auto(world, out)
    first = x.after_round(con, Touched())                          # 시작할 때의 전체 훑기
    assert first is not None and first.written and x.last_sweep is not None
    assert x.after_round(con, Touched()) is None                   # 건드린 것이 없으면 아무것도 하지 않는다

    def review(fid, value, verdict="value") -> list[str]:
        t = Touched()
        save(con, site, st, review_from_field(con, fid, verdict, value, "jp"), touched=t)
        x.mark(t)
        r = x.after_round(con, Touched())
        nothing_missed(world, out)
        return sorted(r.written)

    # ① 계기 값: 첫날의 종료를 고치면 다음 날의 연속성 검산이 바뀐다 — 다음 날의 파일도
    w = review(meter_fields(con, pages[0]["page_id"])["end"], "111.0")
    assert daily_path("2030-01-07") in w and daily_path("2030-01-08") in w and monthly_path("2030-01") in w
    assert daily_path("2030-01-09") not in w                       # 셋째 날은 그대로 (해시가 같다)
    # ② 장비명: 둘째 날의 장비를 고치면 고치기 전 장비(TRUCK)의 다른 날(셋째 날 — 앞 기록이 바뀐다)도 따라온다
    w = review(f"{pages[1]['page_id']}:fields:equipment:-1", "SHOVEL")
    assert daily_path("2030-01-08") in w and daily_path("2030-01-09") in w
    # ③ 운반 횟수 칸 하나: 그 날짜의 일별 파일과 그 달의 파일만
    fid, day = con.execute("SELECT source_field_id, work_date FROM prod_haul WHERE source_role = 'log' AND "
                           "review_status = 'pending' ORDER BY haul_id LIMIT 1").fetchone()
    assert review(fid, "9") == [daily_path(day), monthly_path(day[:7])]
    assert same_as_db(con, site, out) == 4


def test_processing_reports_old_and_new_dates_and_failed_documents(world, tmp_path, monkeypatch):
    """문서의 날짜를 옮기면 옛 날짜의 파일이 그 문서 없이 다시 쓰이고(쪽이 안 남으면 지워지고) 새 날짜에 들어간다.
    다시 처리하다 실패해도 그 문서가 있던 날짜의 파일이 따라온다. 어느 경우에도 끝난 뒤의 전체 훑기는 0."""
    import minedocscan.pipeline.runner as runner

    pipe, site, ids = world["pipe"], world["site"], world["ids"]
    con = pipe.con
    out = tmp_path / "엑셀"
    out.mkdir()
    x = auto(world, out)
    x.after_round(con, Touched())
    path = world["st"].decisions_path(site.root)

    def round_(items=None) -> list[str]:
        if items:
            decs.save(con, path, items, "jp")
        pipe.process_pending(catch=True)
        t, pipe.touched = pipe.touched, Touched()
        r = x.after_round(con, t)
        nothing_missed(world, out)
        return sorted(r.written), sorted(r.deleted)

    # 다른 문서가 있는 날짜로: d(01-08) → 01-09
    w, d = round_([{"target": ids["d_2030-01-08"], "kind": "date", "value": "2030-01-09"}])
    assert daily_path("2030-01-08") in w and daily_path("2030-01-09") in w and not d
    # 쪽이 안 남는 날짜: u3(01-09 의 남은 하나)… 01-09 에는 d 가 있으므로 먼저 d 를 되돌리고 u3 을 01-10 으로
    round_([{"target": ids["d_2030-01-08"], "kind": "date", "value": "2030-01-08"}])
    w, d = round_([{"target": ids["u3_2030-01-09"], "kind": "date", "value": "2030-01-10"}])
    assert d == [daily_path("2030-01-09")] and daily_path("2030-01-10") in w
    # 다시 처리하다 실패: b(01-07) — 그 날짜의 파일이 b 없이 다시 쓰인다
    real = runner.load_pages

    def broken(src, *a, **kw):
        if "b_2030-01-07" in str(src):
            raise OSError("읽을 수 없다")
        return real(src, *a, **kw)

    monkeypatch.setattr(runner, "load_pages", broken)
    decs.request_work(con, [ids["b_2030-01-07"]])
    con.commit()
    w, _d = round_()
    assert daily_path("2030-01-07") in w
    assert con.execute("SELECT status FROM doc_document WHERE document_id = ?", (ids["b_2030-01-07"],)).fetchone()[0] == "failed"
    assert same_as_db(con, site, out) == 4


# ── 작업 스레드와 화면 스레드 ────────────────────────────────────────────────────
def test_screen_review_is_exported_on_the_next_round_without_waking_the_worker(world, tmp_path):
    """serve 의 모양: 작업 스레드(자기 연결)가 바퀴를 돌고, 화면 스레드(자기 연결)의 검수 저장은 건드린 것만 넘긴다 (깨우지 않는다).
    다음 바퀴(여기서는 깨우기 신호로 바퀴를 넘긴다 — poll_seconds 를 기다리지 않게)에 그 날짜의 파일이 바뀐다."""
    from minedocscan.cli import _screen_apps

    pipe, site, st = world["pipe"], world["site"], world["st"]
    out = tmp_path / "엑셀 폴더"
    out.mkdir()
    x = auto(world, out)
    worker = Worker(pipe, after={"excel": x})
    screen = open_db(st.resolved_db_url)

    class Wake(threading.Event):                                  # 깨우기 신호를 몇 번 보냈나 (기다림이 지운 뒤에도 센다)
        sets = 0

        def set(self):
            self.sets += 1
            super().set()

    stop, wake = threading.Event(), Wake()
    ops, app = _screen_apps(screen, site, st, "jp", worker, wake)   # serve 와 같은 연결 (운영 화면 + 검수 화면)
    rounds: list[dict] = []
    done = [threading.Event() for _ in range(3)]

    def on_round(r):
        rounds.append(r)
        done[min(len(rounds), 3) - 1].set()

    t = threading.Thread(target=worker.run_forever, args=(stop, 3600.0, wake, on_round), daemon=True)
    t.start()
    try:
        assert done[0].wait(120)                                  # 첫 바퀴: 전체 훑기
        assert rounds[0]["excel"]["written"] == 4                # 일별 셋 + 월별 하나
        fid, day = screen.execute("SELECT source_field_id, work_date FROM prod_haul WHERE source_role = 'log' AND "
                                  "review_status = 'pending' ORDER BY haul_id LIMIT 1").fetchone()
        app.post_review({"field_id": fid, "verdict": "value", "value": "7"})
        assert wake.sets == 0 and len(rounds) == 1                # 검수 저장은 작업 스레드를 깨우지 않는다
        wake.set()                                                # poll_seconds 가 지난 것처럼
        assert done[1].wait(120)
        assert rounds[1]["excel"]["written"] == 2                 # 그 날짜의 일별 파일과 그 달의 파일
    finally:
        stop.set()
        wake.set()
        t.join(60)
    try:
        assert not t.is_alive()
        assert same_as_db(pipe.con, site, out) == 4
        n = wake.sets                                             # 같은 연결에서 결정은 깨운다 (깨우기 신호가 이어져 있다)
        doc = world["ids"]["d_2030-01-08"]
        assert ops.post_decision({"items": [{"target": doc, "kind": "discard"}], "confirm": True})["ok"]
        assert wake.sets == n + 1 and x.box.take().documents == {doc}
    finally:
        screen.close()


def test_full_sweep_catches_reviews_from_another_process(world, tmp_path):
    """다른 프로세스(review serve)가 저장한 검수는 건드린 것으로 오지 않는다 — sweep_minutes 가 지나면 전체 훑기가 잡는다 (시계 주입)."""
    con, site, st = world["pipe"].con, world["site"], world["st"]
    out = tmp_path / "out"
    out.mkdir()
    clock = Clock()
    x = auto(world, out, sweep=30, clock=clock)
    x.after_round(con, Touched())
    other = open_db(st.resolved_db_url)
    fid, day = other.execute("SELECT source_field_id, work_date FROM prod_haul WHERE source_role = 'log' AND "
                             "review_status = 'pending' ORDER BY haul_id LIMIT 1").fetchone()
    save(other, site, st, review_from_field(other, fid, "value", "4", "jp"))
    other.close()
    clock.t += 29 * 60
    assert x.after_round(con, Touched()) is None                   # 아직 — 건드린 것도 없다
    clock.t += 2 * 60
    r = x.after_round(con, Touched())
    assert sorted(r.written) == [daily_path(day), monthly_path(day[:7])]
    clock.t += 1
    assert x.after_round(con, Touched()) is None


# ── 쓰지 못해도, 폴더가 없어져도 ───────────────────────────────────────────────────
def test_failed_files_and_a_missing_folder_are_retried_and_the_home_counts(world, tmp_path, monkeypatch):
    import minedocscan.export.writer as w

    pipe, site, st = world["pipe"], world["site"], world["st"]
    con = pipe.con
    out = tmp_path / "out"
    out.mkdir()
    x = auto(world, out)
    worker = Worker(pipe, after={"excel": x})
    ops = OpsApp(con, site, st, "jp", worker=worker, excel=x)
    worker.run_once()
    assert ops.home_json()["export"] == x.status and x.status["written"] == 4 and x.status["failed"] == 0
    # 엑셀이 파일을 열고 있다 — 그 파일만 "쓰지 못함", 바퀴는 끝나고 다음 바퀴에 다시
    fid, day = con.execute("SELECT source_field_id, work_date FROM prod_haul WHERE source_role = 'log' AND "
                           "review_status = 'pending' ORDER BY haul_id LIMIT 1").fetchone()
    t = Touched()
    save(con, site, st, review_from_field(con, fid, "value", "6", "jp"), touched=t)
    x.mark(t)
    real = w._replace

    def locked(src, dst):
        if str(dst).endswith(day + ".xlsx"):
            raise PermissionError("열려 있다")
        return real(src, dst)

    monkeypatch.setattr(w, "_replace", locked)
    r = worker.run_once()
    assert r["excel"]["failed"] == 1 and ops.home_json()["export"]["failed"] == 1
    assert "쓰지 못함 1" in format_round(r)
    monkeypatch.setattr(w, "_replace", real)
    x.clock.t += 61                                                # retry_seconds 가 지나면
    r = worker.run_once()                                          # 건드린 것이 없어도 쓰지 못한 날짜를 다시 본다
    assert r["excel"]["written"] == 1 and ops.home_json()["export"]["failed"] == 0
    nothing_missed(world, out)
    # 엑셀 폴더가 없어졌다 — 처리는 계속되고, 그 자리에 폴더를 만들지 않는다
    import shutil

    shutil.rmtree(out)
    decs.save(con, st.decisions_path(site.root), [{"target": world["ids"]["d_2030-01-08"], "kind": "discard"}], "jp")
    r = worker.run_once()
    assert r["processed"] == 1 and r["excel"]["missing_dir"] and not out.exists()
    assert ops.home_json()["export"]["missing_dir"] and "폴더가 없습니다" in format_round(r)
    out.mkdir()                                                    # 돌아오면 그 바퀴에 밀린 날짜를 쓴다
    r = worker.run_once()
    assert r["excel"]["written"] and not r["excel"]["missing_dir"]
    assert x.sweep.due()                                           # 기록이 없다 — 나머지는 새 조각 바퀴가 다시 쓴다
    for _ in range(5):
        worker.run_once()
        if not x.sweep.running:
            break
    assert same_as_db(con, site, out) == 4


def test_without_settings_nothing_is_written_and_in_repo_is_refused(world, tmp_path, capsys):
    from pathlib import Path

    from minedocscan.cli import _round_jobs

    pipe, site = world["pipe"], world["site"]
    x = AutoExport(world["st"], site)                              # excel_dir 없음
    assert not x.enabled and x.reason == "off" and x.after_round(pipe.con, Touched(everything=True)) is None
    r = Worker(pipe, after={"excel": x}).run_once()
    assert "excel" not in r
    repo = Path(__file__).resolve().parents[1]
    jobs = _round_jobs(replace(world["st"], excel_dir=repo / "out" / "엑셀"), site)
    assert jobs["excel"].reason == "refused" and "켜지 않습니다" in capsys.readouterr().err
    assert not (repo / "out" / "엑셀").exists()
    ops = OpsApp(pipe.con, site, world["st"], "jp", watching=False)
    assert ops.home_json()["export"] == {"enabled": False, "reason": "no_watch"}


def test_run_says_it_does_not_export(world, tmp_path, capsys, monkeypatch):
    from minedocscan.cli import main

    st = world["st"]
    out = tmp_path / "엑셀"
    out.mkdir()
    common = ["--site", str(st.site), "--archive-root", str(st.archive_root), "--work-root", str(st.work_root)]
    monkeypatch.setenv("MINEDOCSCAN_EXCEL_DIR", str(out))
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(st.reviews))
    main(["run", str(st.archive_root / "a_2030-01-07.pdf"), "--skip-existing", *common])
    assert "run 은 내보내지 않습니다" in capsys.readouterr().out
    assert list(out.iterdir()) == []


# ── 내려받기 ──────────────────────────────────────────────────────────────────
def test_downloads_equal_the_model(world, tmp_path):
    con, site, st = world["pipe"].con, world["site"], world["st"]
    ops = OpsApp(con, site, st, "jp")
    data, name = ops.export_xlsx("day", {"date": "2030-01-07"})
    assert name == "2030-01-07.xlsx" and data[:2] == b"PK"
    f = tmp_path / name
    f.write_bytes(data)
    with read_txn(con):
        book = daily_book(con, site, "2030-01-07")
    got = read_values(f)
    for s in book["sheets"]:
        want, have = model_rows(s), got[s["name"]]
        for i, row in enumerate(s["rows"]):
            if any(c[1].startswith("stamp") for c in row):
                have[i], want[i] = have[i][:1], want[i][:1]
        assert have == want, s["name"]
    data, name = ops.export_xlsx("month", {"month": "2030-01"})
    assert name == "2030-01.xlsx" and data[:2] == b"PK"
    f = tmp_path / name
    f.write_bytes(data)
    days = [r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date LIKE '2030-01-%' ORDER BY 1")]
    with read_txn(con):
        book = monthly_book(con, site, "2030-01", days)
    got = read_values(f)
    for s in book["sheets"]:
        want, have = model_rows(s), got[s["name"]]
        for i, row in enumerate(s["rows"]):
            if any(c[1].startswith("stamp") for c in row):
                have[i], want[i] = have[i][:1], want[i][:1]
        assert have == want, s["name"]
    for kind, params, status in (("day", {"date": "2030-02-30"}, 400), ("day", {"date": "20300107"}, 400),
                                 ("day", {"date": "2031-01-07"}, 404), ("month", {"month": "2030-13"}, 400),
                                 ("month", {"month": "2031-01"}, 404), ("day", {}, 400)):
        with pytest.raises(ApiError) as e:
            ops.export_xlsx(kind, params)
        assert e.value.status == status, (kind, params)
    assert not list(tmp_path.glob("*.tmp")) and not (st.work_root / "daily").exists()      # 디스크에 쓰지 않는다


# ── 접수 묶음을 끝까지 (watch --once) ─────────────────────────────────────────────
@pytest.mark.slow
def test_watch_once_with_an_excel_folder_from_the_inbox_to_the_files(tmp_path, monkeypatch):
    """synth --intake 를 excel_dir 를 준 watch --once 로: 날짜마다 일별 파일과 달의 파일, 내용이 DB 와 같다. 날짜를 기다리는 문서의
    날짜를 정하면 그 날짜의 파일이 생기고(바뀌고), 쪽을 버리면 그 날짜의 파일이 바뀐다. 한 번 더 돌면 아무 파일도 쓰지 않는다."""
    import json

    from conftest import fast_imaging
    from minedocscan.cli import main
    from minedocscan.forms.sitepack import SitePack
    from minedocscan.tools.synth import generate

    fast_imaging(monkeypatch)
    r = generate(tmp_path / "synth", days=4, seed=0, intake=True)
    out, archive, work = tmp_path / "엑셀 폴더", tmp_path / "archive", tmp_path / "work"
    out.mkdir()
    archive.mkdir()
    monkeypatch.setenv("MINEDOCSCAN_EXCEL_DIR", str(out))
    monkeypatch.setenv("MINEDOCSCAN_INBOX", str(r.root / "inbox"))
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(tmp_path / "기록" / "reviews.jsonl"))
    common = ["--site", str(r.site), "--archive-root", str(archive), "--work-root", str(work)]
    site = SitePack(r.site)

    def watch() -> dict:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main(["watch", "--once", "--json", "--settle-seconds", "0", "--give-up-seconds", "0", *common]) == 0
        return json.loads(buf.getvalue())["watch"]

    first = watch()
    con = open_db(f"sqlite:///{work / 'minedocscan.db'}")
    assert first["excel"]["written"] == same_as_db(con, site, out) and first["needs_date"]
    decisions = r.truth["intake"]["decisions"]
    date_items = [d for d in decisions if d["kind"] == "date"]
    others = [d for d in decisions if d["kind"] != "date"]
    assert date_items and others
    path = intake_settings(r, archive, work, tmp_path).decisions_path(site.root)
    decs.save(con, path, date_items, "jp")
    day = date_items[0]["value"]
    second = watch()
    assert second["excel"]["written"] >= 2 and (out / daily_path(day)).is_file()
    same_as_db(con, site, out)
    before = {p: (out / p).stat().st_mtime_ns for p in (p.relative_to(out).as_posix() for p in out.rglob("*.xlsx"))}
    decs.save(con, path, others, "jp")
    third = watch()
    changed = {p for p, t in before.items() if (out / p).exists() and (out / p).stat().st_mtime_ns != t}
    assert third["excel"]["written"] and changed
    same_as_db(con, site, out)
    fourth = watch()
    assert fourth["excel"]["written"] == 0 and fourth["excel"]["deleted"] == 0
    con.close()


def intake_settings(r, archive, work, root):
    from minedocscan.config import Settings

    return Settings(site=r.site, archive_root=archive, work_root=work, reviews=root / "기록" / "reviews.jsonl")


def test_info_shows_the_export_settings(tmp_path, monkeypatch, capsys):
    import json

    from minedocscan.cli import main

    monkeypatch.setenv("MINEDOCSCAN_EXCEL_DIR", str(tmp_path / "엑셀"))
    cfg = tmp_path / "minedocscan.toml"
    cfg.write_text("[export]\nsweep_minutes = 5\nmachine_values = true\nretry_seconds = 30\n", encoding="utf-8")
    assert main(["info", "--config", str(cfg), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["export"]
    assert data == {"excel_dir": str(tmp_path / "엑셀"), "sweep_minutes": 5.0, "machine_values": True, "retry_seconds": 30.0}
    for bad in ("sweep_minutes = -1", "retry_seconds = -1"):
        cfg.write_text(f"[export]\n{bad}\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            main(["info", "--config", str(cfg)])


def test_a_decision_from_the_screen_marks_its_documents(world, tmp_path):
    """화면에서 저장한 결정은 그 문서를 건드린 것으로 넘긴다 — 처리가 그 문서를 하지 못해도(원본에 닿지 않는다) 그 날짜의 엑셀이 따라온다."""
    pipe, site, st = world["pipe"], world["site"], world["st"]
    out = tmp_path / "out"
    out.mkdir()
    x = auto(world, out)
    x.after_round(pipe.con, Touched())
    ops = OpsApp(pipe.con, site, st, "jp", excel=x)
    doc = world["ids"]["d_2030-01-08"]
    assert ops.post_decision({"items": [{"target": doc, "kind": "discard"}], "confirm": True})["ok"]
    assert doc in x.box.take().documents
    # 버린 문서(쪽이 없다 — 문서의 날짜만 남는다)를 원본에 닿지 않을 때 되살린다: 처리는 못 하지만 그 날짜의 "다시 처리 대기"가 1 이 된다
    pipe.process_pending()
    t, pipe.touched = pipe.touched, Touched()
    x.after_round(pipe.con, t)
    assert pipe.con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id = ?", (doc,)).fetchone()[0] == 0
    moved = tmp_path / "moved"
    moved.mkdir()
    shutil.move(str(world["scans"] / "d_2030-01-08.pdf"), moved / "d_2030-01-08.pdf")
    assert ops.post_decision({"items": [{"target": doc, "kind": "restore"}], "confirm": True})["ok"]
    pipe.process_pending()
    assert pipe.summary["unreachable"][-1] == doc and doc in pipe.touched.documents      # 처리도 건드린 것으로 남긴다
    t, pipe.touched = pipe.touched, Touched()
    r = x.after_round(pipe.con, t)
    assert r is not None and set(r.written) == {daily_path("2030-01-08"), monthly_path("2030-01")}
    assert same_as_db(pipe.con, site, out) and export_excel(pipe.con, site, out, full=True).written == []
    pipe.process_pending()                                    # 같은 요청 번호로 다시 닿지 않으면 다시 건드리지 않는다
    assert not pipe.touched.documents
    shutil.move(str(moved / "d_2030-01-08.pdf"), world["scans"] / "d_2030-01-08.pdf")


# ── 사본의 주인과 더러운 범위의 구멍 (tasks/0009 4.1 다·바) ─────────────────────────────────
def record_shas(out) -> dict[str, str]:
    """기록 파일의 파일 → 모델의 해시."""
    from minedocscan.export.writer import load_record

    files, lost, _site = load_record(out)
    assert not lost
    return {k: v["sha"] for k, v in files.items()}


def full_export_shas(con, site, root) -> dict[str, str]:
    """같은 DB 를 새 폴더에 전부 내보낸 것의 해시 — "엑셀 폴더 = 그 DB 를 전부 내보낸 것"의 기준."""
    fresh = root / "견줄 폴더"
    fresh.mkdir()
    r = export_excel(con, site, fresh, full=True)
    assert not r.failed and r.written
    return record_shas(fresh)


def test_a_folder_of_another_site_is_said_once_and_left_alone(world, tmp_path):
    """기록 파일이 다른 사이트의 것이면 자동 내보내기는 바퀴마다 아무것도 쓰거나 지우지 않는다: 첫 바퀴만 요약(Result.other_site,
    "다른 사이트의 폴더"), 다음 바퀴는 None — 홈의 상태에는 늘 보인다. 기록 파일을 지우면(안내대로) 다음 바퀴에 전부 쓴다."""
    import json

    from minedocscan.export.model import MODEL_VERSION
    from minedocscan.export.writer import RECORD_NAME

    pipe, site, st = world["pipe"], world["site"], world["st"]
    con = pipe.con
    out = tmp_path / "엑셀"
    out.mkdir()
    rec = out / RECORD_NAME
    rec.write_text(json.dumps({"model": MODEL_VERSION, "site": "OTHER-SITE-X", "files": {}}), encoding="utf-8")
    raw = rec.read_bytes()
    x = auto(world, out)
    assert x.enabled and x.status["other_site"] is False
    worker = Worker(pipe, after={"excel": x})
    ops = OpsApp(con, site, st, "jp", worker=worker, excel=x)
    first = x.after_round(con, Touched())                               # 시작할 때의 전체 훑기
    assert first is not None and first.other_site and (first.written, first.deleted, first.failed) == ([], [], [])
    assert x.status["other_site"] and ops.home_json()["export"]["other_site"]
    text = format_round({"processed": 0, "excel": first.as_dict()})
    assert "다른 사이트의 폴더" in text and "OTHER-SITE-X" not in text and site.declared_name not in text
    assert x.after_round(con, Touched(everything=True)) is None         # 요약은 처음 한 번
    assert x.status["other_site"] and x.status["rounds"] == 2
    r = worker.run_once()
    assert "excel" not in r and "excel_error" not in r and ops.home_json()["export"]["other_site"]
    assert [p.name for p in out.rglob("*") if p.is_file()] == [RECORD_NAME] and rec.read_bytes() == raw
    rec.unlink()                                                        # 이 사이트로 바꾼다
    r = worker.run_once()
    assert r["excel"]["written"] == 4 and r["excel"]["record_lost"] and not r["excel"]["other_site"]
    assert not x.status["other_site"] and same_as_db(con, site, out) == 4


def test_a_document_without_pages_reports_its_old_document_date(world, tmp_path):
    """쪽이 없는 문서(버린 문서)의 문서 날짜를 결정이 D1 → D2 로 옮긴다: 처리 전에는 D1 의 파일에 "다시 처리 대기"가 하나 있고,
    처리한 뒤 건드린 날짜에 D1·D2 가 다 있어 더러운 바퀴가 D1 의 파일을 다시 쓴다 — 폴더 = 전부 내보낸 것 (4.1 바)."""
    pipe, site, st, ids = world["pipe"], world["site"], world["st"], world["ids"]
    con = pipe.con
    doc, d1, d2 = ids["d_2030-01-08"], "2030-01-08", "2030-01-09"
    path = st.decisions_path(site.root)
    decs.save(con, path, [{"target": doc, "kind": "discard"}], "jp")
    pipe.process_pending()
    pipe.touched = Touched()

    def doc_state() -> tuple:
        n = con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id = ?", (doc,)).fetchone()[0]
        return (n, *con.execute("SELECT status, work_date FROM doc_document WHERE document_id = ?", (doc,)).fetchone())

    assert doc_state() == (0, "discarded", d1)
    out = tmp_path / "엑셀"
    out.mkdir()
    x = auto(world, out)
    x.after_round(con, Touched())                                       # 시작할 때의 전체 훑기
    ops = OpsApp(con, site, st, "jp", excel=x)
    assert ops.post_decision({"items": [{"target": doc, "kind": "date", "value": d2}], "confirm": True})["ok"]
    r = x.after_round(con, Touched())                                   # 화면이 넘긴 문서: D1 에 "다시 처리 대기" 1
    assert daily_path(d1) in r.written and daily_path(d2) not in r.written
    pipe.process_pending()
    t, pipe.touched = pipe.touched, Touched()
    assert doc_state() == (0, "discarded", d2)
    assert {d1, d2} <= t.dates and not t.everything
    r = x.after_round(con, t)
    assert daily_path(d1) in r.written                                  # 대기가 0 으로 — 옛 문서 날짜의 파일
    assert record_shas(out) == full_export_shas(con, site, tmp_path)
    nothing_missed(world, out)


def test_a_changed_equipment_master_rewrites_the_inspection_sheets_of_every_date(world, tmp_path):
    """점검표 템플릿의 장비 행(모델)을 바꾼 사이트 팩으로 점검표 문서 하나를 다시 처리하면 eq_equipment 가 바뀐다 — 모든 날짜의 점검
    시트에 나오므로 건드린 것은 "전부"이고, 더러운 바퀴가 다른 날짜(그 문서가 없는 날짜)의 점검 시트도 다시 쓴다 (4.1 바)."""
    import yaml

    from minedocscan.export import labels as L
    from minedocscan.forms.equipment import is_equipment_row
    from minedocscan.forms.sitepack import SitePack
    from minedocscan.pipeline import Pipeline

    pipe, site, st, ids = world["pipe"], world["site"], world["st"], world["ids"]
    con = pipe.con
    # 점검표가 있는 날짜를 둘로: c(점검표 + T01)를 01-08 로 (지금의 사이트 팩으로)
    decs.save(con, st.decisions_path(site.root), [{"target": ids["c_2030-01-07"], "kind": "date", "value": "2030-01-08"}], "jp")
    pipe.process_pending()
    pipe.touched = Touched()
    insp_days = sorted(r[0] for r in con.execute("SELECT DISTINCT inspection_date FROM insp_daily"))
    assert insp_days == ["2030-01-07", "2030-01-08"]
    # 장비 행 하나의 모델만 바꾼 사이트 팩 (키는 그대로 — 같은 장비 ID)
    root = tmp_path / "site2"
    shutil.copytree(site.root, root)
    ty = root / "templates" / "synth_inspection" / "template.yaml"
    spec = yaml.safe_load(ty.read_text(encoding="utf-8"))
    row = next(r for r in spec["regions"][0]["rows"] if is_equipment_row(r))
    row["model"] = new_model = f"{row['model']}-X2"
    ty.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    site2 = SitePack(root)
    st2 = replace(st, site=root)
    out = tmp_path / "엑셀"
    out.mkdir()
    x = AutoExport(replace(st2, excel_dir=out, export_sweep_minutes=0), site2, clock=Clock(),
                   now=lambda: "2030-02-01T00:00:00Z")
    assert x.after_round(con, Touched()).written                        # 시작할 때의 전체 훑기 (장비 마스터는 옛 모델)
    pipe2 = Pipeline(st2, site=site2, con=con)
    decs.request_work(con, [ids["a_2030-01-07"]])
    con.commit()
    assert pipe2.process_pending() >= 1
    t = pipe2.touched
    assert t.everything
    narrow = Touched(dates=set(t.dates), documents=set(t.documents), removed=set(t.removed))
    assert "2030-01-08" not in narrow.all_dates(con)                    # "전부"가 아니면 01-08 은 범위 밖이다
    hid = con.execute("SELECT hid FROM eq_equipment WHERE equipment_key = ?", (str(row["key"]),)).fetchone()[0]
    assert new_model in hid
    r = x.after_round(con, t)
    assert {daily_path(d) for d in insp_days} | {monthly_path("2030-01")} <= set(r.written)
    for d in insp_days:                                                 # 두 날짜의 점검 시트에 새 모델
        sheet = read_values(out / daily_path(d))[L.SHEETS["inspection"]]
        assert any(isinstance(v, str) and new_model in v for row_ in sheet for v in row_), d
    assert record_shas(out) == full_export_shas(con, site2, tmp_path)


# ── 조각으로 나눈 전체 훑기 (tasks/0009 4.2 가) ───────────────────────────────────────
def two_months(world) -> None:
    """d 문서를 2030-02-08 로 옮겨 달이 둘이 되게 한다 (결정 → 처리)."""
    pipe, site, st = world["pipe"], world["site"], world["st"]
    decs.save(pipe.con, st.decisions_path(site.root), [{"target": world["ids"]["d_2030-01-08"], "kind": "date",
                                                       "value": "2030-02-08"}], "jp")
    pipe.process_pending()
    pipe.touched = Touched()


def test_a_sweep_goes_one_month_per_round_and_ends_equal_to_a_full_export(world, tmp_path):
    """전체 훑기는 바퀴마다 달 하나씩: 달의 목록은 작업 DB 의 달과 기록·폴더에만 있는 달 — 기록에만 있는 달의 파일도 지워진다.
    도는 동안 홈에 "처음 훑는 중 n/N", 다 돌면 마지막 전체 훑기의 시각. 다 돈 폴더 = 전부 내보낸 것. sweep_minutes = 0 이면 시작할 때
    한 바퀴만."""
    import json

    from minedocscan.export.model import MODEL_VERSION
    from minedocscan.export.writer import RECORD_NAME

    con, site, st = world["pipe"].con, world["site"], world["st"]
    two_months(world)
    out = tmp_path / "엑셀"
    out.mkdir()
    export_excel(con, site, out, full=True)
    stray = out / "daily" / "2029-12" / "2029-12-31.xlsx"            # 기록에만 있는 달 (그 날짜의 쪽은 없다)
    stray.parent.mkdir(parents=True)
    shutil.copy(out / daily_path("2030-01-07"), stray)
    rec = json.loads((out / RECORD_NAME).read_text(encoding="utf-8"))
    rec["files"]["daily/2029-12/2029-12-31.xlsx"] = {"sha": "0" * 64, "model": MODEL_VERSION, "written_at": "x"}
    (out / RECORD_NAME).write_text(json.dumps(rec), encoding="utf-8")
    x = auto(world, out)
    ops = OpsApp(con, site, st, "jp", excel=x)
    r = x.after_round(con, Touched())                                   # 첫 조각: 2029-12
    assert r.deleted == ["daily/2029-12/2029-12-31.xlsx"] and not stray.exists()
    assert x.status["sweep"] == {"done": 1, "total": 3, "first": True} and x.status["last_sweep_at"] is None
    assert ops.home_json()["export"]["sweep"] == {"done": 1, "total": 3, "first": True}
    r = x.after_round(con, Touched())                                   # 2030-01
    assert x.status["sweep"]["done"] == 2 and not any("2030-02" in p for p in r.written)
    x.after_round(con, Touched())                                       # 2030-02 — 다 돌았다
    assert x.status["sweep"] is None and x.status["last_sweep_at"] == "2030-02-01T00:00:00Z" and x.last_sweep == x.clock()
    assert record_shas(out) == full_export_shas(con, site, tmp_path)
    x.clock.t += 10_000
    assert x.after_round(con, Touched()) is None                        # sweep_minutes = 0 — 다시 시작하지 않는다


def test_an_empty_folder_and_a_lost_record_are_written_one_month_per_round(world, tmp_path):
    """처음 쓰는 빈 폴더(기록 파일이 없다)도 한 번에 전부 쓰지 않고 바퀴마다 달 하나씩. 도는 사이에 기록을 잃으면(누가 지웠다) 그 바퀴는
    더러운 범위와 그 조각만 하고 새 조각 바퀴를 시작한다 — 다 돌면 전부 내보낸 것과 같다. 명령(export_excel)은 지금처럼 한 번에 전부."""
    from minedocscan.export.writer import RECORD_NAME

    con, site = world["pipe"].con, world["site"]
    two_months(world)
    out = tmp_path / "엑셀"
    out.mkdir()
    x = auto(world, out)
    r = x.after_round(con, Touched())                                   # 2030-01 만
    assert r.record_lost and r.written and all("2030-01" in p for p in r.written), r.as_dict()
    assert x.status["sweep"] == {"done": 1, "total": 2, "first": True}
    (out / RECORD_NAME).unlink()                                        # 도는 사이에 기록을 잃었다
    r = x.after_round(con, Touched())                                   # 2030-02 를 하고 — 새 바퀴로
    assert r.record_lost and all("2030-02" in p for p in r.written) and x.sweep.due()
    n = 0
    while True:
        x.after_round(con, Touched())
        n += 1
        if not x.sweep.running:
            break
    assert n == 2 and x.status["last_sweep_at"]
    assert record_shas(out) == full_export_shas(con, site, tmp_path)
    one = tmp_path / "명령"
    one.mkdir()
    r = export_excel(con, site, one, days=["2030-01-07"])               # 명령: 기록이 없으면 전부
    assert r.record_lost and any("2030-02" in p for p in r.written)


def test_watch_once_does_the_whole_sweep_in_its_one_round(world, tmp_path):
    """watch --once 는 다음 바퀴가 없다 — 할 때가 된 전체 훑기를 조각으로 나누지 않고 그 바퀴에 한 번에 한다 (나누면 실행마다 첫 달만
    훑어 다음 달은 영영 훑지 않는다)."""
    from minedocscan.cli import _round_jobs

    con, site, st = world["pipe"].con, world["site"], world["st"]
    two_months(world)
    out = tmp_path / "엑셀"
    out.mkdir()
    x = _round_jobs(replace(st, excel_dir=out), site, once=True)["excel"]
    assert x.once and not _round_jobs(replace(st, excel_dir=out), site)["excel"].once
    x.after_round(con, Touched())
    assert x.status["sweep"] is None and x.status["last_sweep_at"]
    assert record_shas(out) == full_export_shas(con, site, tmp_path)


def test_a_cycle_that_could_not_start_starts_again(world, tmp_path):
    """바퀴를 시작하는 바퀴에 아무것도 하지 못했으면(폴더가 없다) 그 바퀴는 없던 것으로 — 폴더가 돌아오면 달의 목록부터 다시 시작해
    끝까지 간다 (멈춰 서지 않는다)."""
    con, site = world["pipe"].con, world["site"]
    two_months(world)
    out = tmp_path / "엑셀"
    x = auto(world, out)
    r = x.after_round(con, Touched())
    assert r.missing_dir and not x.sweep.running and x.sweep.due() and x.status["sweep"] is None
    out.mkdir()
    n = 0
    while True:
        x.after_round(con, Touched())
        n += 1
        if not x.sweep.running:
            break
    assert n == 2 and x.status["last_sweep_at"]
    assert record_shas(out) == full_export_shas(con, site, tmp_path)


def test_an_unreadable_record_shows_on_the_home_and_in_the_summary_once(world, tmp_path):
    """기록 파일을 읽지 못하면(잠겨 있다 — 여기서는 그 이름의 폴더) 홈의 "쓰지 못함"에 들고 요약에는 수가 바뀐 바퀴에만. 풀리면 0."""
    from minedocscan.cli import _worth_showing
    from minedocscan.export.writer import RECORD_NAME

    con, site = world["pipe"].con, world["site"]
    out = tmp_path / "엑셀"
    out.mkdir()
    (out / RECORD_NAME).mkdir()
    x = auto(world, out)
    r = x.after_round(con, Touched())
    assert RECORD_NAME in r.failed and x.status["failed"] == 1 and r.failing == 1
    assert _worth_showing({"excel": r.as_dict()}) and "쓰지 못함 1" in format_round({"excel": r.as_dict()})
    r = x.after_round(con, Touched(dates={"2030-01-07"}))
    assert x.status["failed"] == 1 and not _worth_showing({"excel": r.as_dict()})
    (out / RECORD_NAME).rmdir()
    while True:
        r = x.after_round(con, Touched())
        if not x.sweep.running:
            break
    assert x.status["failed"] == 0 and record_shas(out) == full_export_shas(con, site, tmp_path)


def test_touched_is_kept_when_reading_the_dirty_range_fails(world, tmp_path, monkeypatch):
    """바퀴 끝의 내보내기가 더러운 범위를 읽다가 실패하면(DB 가 잠겼다 …) 건드린 것을 다음 바퀴로 넘긴다 (버리지 않는다)."""
    con = world["pipe"].con
    out = tmp_path / "엑셀"
    out.mkdir()
    x = auto(world, out)
    while True:
        x.after_round(con, Touched())
        if not x.sweep.running:
            break

    def boom(self, con_):
        raise RuntimeError("시험용")

    monkeypatch.setattr(Touched, "all_dates", boom)
    with pytest.raises(RuntimeError):
        x.after_round(con, Touched(dates={"2030-01-07"}, documents={world["ids"]["a_2030-01-07"]}))
    held = x.box.take()
    assert held.dates == {"2030-01-07"} and held.documents == {world["ids"]["a_2030-01-07"]}
    # 바퀴를 시작하는 바퀴에서 실패해도 그 바퀴는 없던 것으로 — 다음에 다시 시작한다 (빈 달 목록으로 멈춰 서지 않는다)
    y = auto(world, tmp_path / "없는 폴더")
    with pytest.raises(RuntimeError):
        y.after_round(con, Touched())
    assert not y.sweep.running and y.sweep.due()
    monkeypatch.undo()


def test_a_sweep_restarts_every_sweep_minutes_and_dirty_dates_ride_along(world, tmp_path):
    """sweep_minutes 마다 새 바퀴. 도는 동안 더러운 날짜는 그 바퀴에 같이 한다 (조각과 겹치면 한 번만 — 파일은 한 번 쓴다)."""
    con, site, st = world["pipe"].con, world["site"], world["st"]
    two_months(world)
    out = tmp_path / "엑셀"
    out.mkdir()
    x = auto(world, out, sweep=1.0)
    x.after_round(con, Touched())
    x.after_round(con, Touched())                                       # 처음 바퀴를 다 돌았다 (달 둘)
    assert x.status["sweep"] is None and x.last_sweep == x.clock()
    assert x.after_round(con, Touched()) is None                        # 1분이 지나지 않았다
    x.clock.t += 61
    fid, day = con.execute("SELECT source_field_id, work_date FROM prod_haul WHERE source_role = 'log' AND review_status = 'pending' "
                           "AND work_date LIKE '2030-01-%' ORDER BY haul_id LIMIT 1").fetchone()
    t = Touched()
    save(con, site, st, review_from_field(con, fid, "value", "7", "jp"), touched=t)
    r = x.after_round(con, t)                                           # 새 바퀴의 첫 조각(2030-01) + 그 달의 더러운 날짜
    assert x.status["sweep"] == {"done": 1, "total": 2, "first": False}
    assert sorted(r.written) == [daily_path(day), monthly_path("2030-01")]
    r = x.after_round(con, Touched())
    assert r.written == [] and x.status["sweep"] is None
    assert record_shas(out) == full_export_shas(con, site, tmp_path)


def test_a_file_that_stays_open_is_retried_every_retry_seconds_and_said_once(world, tmp_path, monkeypatch):
    """바꾸지 못하는 파일 하나가 10바퀴 동안 열려 있으면 그 파일의 모델은 retry_seconds 마다만 만들고(그 사이의 바퀴는 만들지 않는다),
    바퀴의 요약 줄은 쓰지 못한 파일의 수가 바뀔 때만. 홈의 수는 그대로 보인다 (tasks/0009 4.2 라)."""
    import minedocscan.export.writer as w
    from minedocscan.cli import _worth_showing

    con, site, st = world["pipe"].con, world["site"], world["st"]
    out = tmp_path / "엑셀"
    out.mkdir()
    x = auto(world, out)
    x.after_round(con, Touched())                                       # 처음 바퀴
    fid, day = con.execute("SELECT source_field_id, work_date FROM prod_haul WHERE source_role = 'log' AND "
                           "review_status = 'pending' ORDER BY haul_id LIMIT 1").fetchone()
    real_replace, real_book = w._replace, w.daily_book
    built = []
    locked = (day + ".xlsx", day[:7] + ".xlsx")                        # 그 날짜의 파일과 그 달의 파일을 엑셀이 열고 있다
    monkeypatch.setattr(w, "_replace", lambda src, dst: (_ for _ in ()).throw(PermissionError("열려 있다"))
                        if str(dst).endswith(locked) else real_replace(src, dst))
    monkeypatch.setattr(w, "daily_book", lambda con_, site_, d, *a, **kw: (built.append(d) if d == day else None) or
                        real_book(con_, site_, d, *a, **kw))
    lines = []
    for n in range(10):                                                 # 10바퀴, 바퀴마다 7초 — 그 날짜의 칸을 바퀴마다 검수한다
        t = Touched()
        save(con, site, st, review_from_field(con, fid, "value", str(3 + n % 2), "jp"), touched=t)
        out_ = {"excel": x.after_round(con, t).as_dict()}
        lines.append(_worth_showing(out_))
        assert x.status["failed"] == 2
        x.clock.t += 7
    assert built == [day, day]                                          # 처음과 60초 뒤의 한 번 (7초 × 9 = 63초)
    assert lines == [True] + [False] * 9                                # 요약은 수가 바뀐 처음 바퀴에만
    monkeypatch.setattr(w, "_replace", real_replace)
    x.clock.t += 60
    r = x.after_round(con, Touched())                                   # 닫았다 — 다음 시도에서 쓰고 수가 0 으로
    assert {daily_path(day), monthly_path(day[:7])} <= set(r.written) and x.status["failed"] == 0
    assert _worth_showing({"excel": r.as_dict()})
    assert record_shas(out) == full_export_shas(con, site, tmp_path)


def test_a_meter_review_dirties_only_the_neighbouring_records(world, tmp_path):
    """가동 일보의 계기 칸 하나의 검수 → 그 칸의 날짜와 연속성 행이 바뀐 쪽의 날짜만 (장비의 모든 날짜로 넓히지 않는다 — 4.2 다).
    장비 이름을 고치면 옛 장비의 사슬(앞 기록이 바뀐 셋째 날)과 새 장비의 사슬을 둘 다 잡는다. Touched 에 장비가 없다."""
    con, site, st = world["pipe"].con, world["site"], world["st"]
    pages = chain(world, st)
    assert not hasattr(Touched(), "refs")
    t = Touched()
    save(con, site, st, review_from_field(con, meter_fields(con, pages[2]["page_id"])["start"], "value", "121.0", "jp"), touched=t)
    assert t.all_dates(con) == {"2030-01-08", "2030-01-09"}             # 셋째 날의 시작 — 그 행과 그 행이 가리키는 앞 기록의 날짜
    t = Touched()
    save(con, site, st, review_from_field(con, meter_fields(con, pages[0]["page_id"])["end"], "value", "111.0", "jp"), touched=t)
    assert t.all_dates(con) == {"2030-01-07", "2030-01-08"}             # 첫날의 종료 → 둘째 날의 연속성 (셋째 날은 아니다)
    t = Touched()
    save(con, site, st, review_from_field(con, f"{pages[1]['page_id']}:fields:equipment:-1", "value", "SHOVEL", "jp"), touched=t)
    # 둘째 날이 빠진 TRUCK 사슬 — 셋째 날의 앞 기록이 첫날로 (셋 다), 새 장비(SHOVEL)의 사슬은 둘째 날 하나
    assert t.all_dates(con) == {"2030-01-07", "2030-01-08", "2030-01-09"}
