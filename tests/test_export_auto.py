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
    cfg.write_text("[export]\nsweep_minutes = 5\nmachine_values = true\n", encoding="utf-8")
    assert main(["info", "--config", str(cfg), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["export"]
    assert data == {"excel_dir": str(tmp_path / "엑셀"), "sweep_minutes": 5.0, "machine_values": True}
    cfg.write_text("[export]\nsweep_minutes = -1\n", encoding="utf-8")
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
