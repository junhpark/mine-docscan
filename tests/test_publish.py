"""통합 DB 로 싣기 — 서버 없이 되는 것 (tasks/0008 단계 5, 4.8): 대상의 DDL(schema.sql 에서), 범위와 지문(순수 함수),
연결 함수를 바꿔 끼운 시험(닿지 않는 서버 — 비밀번호가 어디에도 찍히지 않는다, 다시 연결하는 간격), 드라이버가 없을 때.
PostgreSQL 이 있어야 하는 것은 test_publish_pg.py (-m postgres). 실제 드라이버의 시간 제한을 기다리는 시험 하나도 postgres 작업에서.
"""
from __future__ import annotations

import json
import os
import re
import select
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from minedocscan.intake.worker import Worker, format_round
from minedocscan.publish import core, ddl
from minedocscan.publish.auto import AutoPublish
from minedocscan.publish.scopes import (
    DATE_TABLES,
    DOC_TABLES,
    WHOLE_TABLES,
    all_keys,
    build,
    orphans,
)
from minedocscan.review.ops import OpsApp
from minedocscan.store.db import PUBLISH_SKIP_COLUMNS, PUBLISH_TABLES, SCHEMA_PATH
from minedocscan.touched import Touched

SECRET = "s3cr3t-p4ss"
URL = f"postgresql://writer:{SECRET}@db.example.invalid:5432/site"


# ── DDL ─────────────────────────────────────────────────────────────────────
def test_target_ddl_comes_from_schema_sql():
    stmts = ddl.create_statements("minedocscan")
    text = "\n".join(stmts)
    made = re.findall(r'CREATE TABLE "minedocscan"\."(\w+)"', text)
    assert made == [*PUBLISH_TABLES, "pub_state", "pub_meta"]
    assert "REFERENCES" not in text and "FOREIGN" not in text                  # 외래 키를 걸지 않는다
    for t in ("doc_page_sig", "doc_review", "doc_decision", "meta_schema"):
        assert f'"{t}"' not in text
    for c in PUBLISH_SKIP_COLUMNS:
        assert f'"{c}"' not in text
    assert " REAL" not in text and " INTEGER" not in text                       # 형을 넓혔다
    mem = sqlite3.connect(":memory:")
    mem.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for t in ddl.tables():
        info = {r[1]: r[2] for r in mem.execute(f"PRAGMA table_info({t.name})")}
        assert [c.name for c in t.columns] == [c for c in info if c not in PUBLISH_SKIP_COLUMNS]
        for c in t.columns:
            assert c.type == {"REAL": "DOUBLE PRECISION", "INTEGER": "BIGINT", "TEXT": "TEXT"}[info[c.name]]
        block = next(s for s in stmts if f'"minedocscan"."{t.name}" (' in s and s.startswith("CREATE TABLE"))
        assert "PRIMARY KEY (" + ", ".join(f'"{c}"' for c in t.pk) + ")" in block
    assert 'UNIQUE ("equipment_key")' in text and 'CREATE INDEX "ix_doc_field_page"' in text
    with pytest.raises(ValueError):
        ddl.create_statements('x"; DROP TABLE y; --')


# ── 범위와 지문 ───────────────────────────────────────────────────────────────
def scopes_of(con) -> dict:
    keys = all_keys(con)
    return {sc.id: sc for kind in ("document", "date", "whole") for sc in build(con, kind, keys[kind])}


def test_every_row_is_in_exactly_one_scope(world):
    con = world["pipe"].con
    assert orphans(con) == {}
    scopes = scopes_of(con)
    for t in PUBLISH_TABLES:
        n = sum(len(sc.rows.get(t, [])) for sc in scopes.values())
        assert n == con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], t
    kinds = {k for k, _ in scopes}
    assert kinds == {"document", "date", "whole"} and set(DOC_TABLES) | set(DATE_TABLES) | set(WHOLE_TABLES) == set(PUBLISH_TABLES)
    # 어느 범위에도 들지 않는 행이 있으면 싣지 않는다 (수로 알린다)
    c2 = sqlite3.connect(":memory:")
    con.backup(c2)
    c2.execute("UPDATE insp_daily SET page_id = NULL WHERE rowid = (SELECT MIN(rowid) FROM insp_daily)")
    assert orphans(c2) == {"insp_daily": 1}


def test_fingerprints_change_only_for_the_changed_scope(world):
    con = world["pipe"].con
    a = {k: sc.fingerprint() for k, sc in scopes_of(con).items()}
    assert a == {k: sc.fingerprint() for k, sc in scopes_of(con).items()}       # 같은 DB 면 같은 지문
    doc = world["ids"]["a_2030-01-07"]
    c2 = sqlite3.connect(":memory:")
    con.backup(c2)
    c2.row_factory = sqlite3.Row
    c2.execute("UPDATE doc_field SET value_final = 'x' WHERE field_id = (SELECT MIN(field_id) FROM doc_field WHERE page_id = ?)",
               (f"{doc}-p1",))
    b = {k: sc.fingerprint() for k, sc in scopes_of(c2).items()}
    assert {k for k in a if a[k] != b[k]} == {("document", doc)}
    c2.execute("UPDATE xcheck_haul SET log_trips = 99 WHERE rowid = (SELECT MIN(rowid) FROM xcheck_haul)")
    day = c2.execute("SELECT work_date FROM xcheck_haul WHERE rowid = (SELECT MIN(rowid) FROM xcheck_haul)").fetchone()[0]
    b2 = {k: sc.fingerprint() for k, sc in scopes_of(c2).items()}
    assert {k for k in a if a[k] != b2[k]} == {("document", doc), ("date", day)}
    # 실수는 정확한 표현으로 — 1.0 과 1 은 다른 값이다
    c2.execute("UPDATE doc_page SET classify_margin = 1.0000000000000002 WHERE page_id = ?", (f"{doc}-p1",))
    assert scopes_of(c2)[("document", doc)].fingerprint() != b2[("document", doc)]
    # 시각·요청 번호·경로는 지문에 들지 않는다 (--fresh 마다 달라진다)
    c3 = sqlite3.connect(":memory:")
    con.backup(c3)
    c3.row_factory = sqlite3.Row
    c3.execute("UPDATE doc_document SET created_at = 'x', received_at = 'y', work_requested = 9, work_done = 9, source_path = '/z'")
    assert {k: sc.fingerprint() for k, sc in scopes_of(c3).items()} == a


def test_fresh_db_gives_the_same_fingerprints(world, tmp_path):
    """같은 파일·검수·결정으로 처음부터 만든 DB 의 문서·날짜 범위의 지문이 같다 (내용이 같으면 지문이 같다)."""
    from test_reprocess import fresh_of

    st, site = world["st"], world["site"]
    fresh = fresh_of(st, site, world["scans"], tmp_path, "fresh")
    a = {k: sc.fingerprint() for k, sc in scopes_of(world["pipe"].con).items()}
    b = {k: sc.fingerprint() for k, sc in scopes_of(fresh.con).items()}
    assert a == b


def test_unsuitable_values_are_counted():
    from minedocscan.publish.scopes import Scope

    assert Scope("document", "d", {"doc_field": [("a\x00b", 1.5, None)]}).unsuitable() == 1
    assert Scope("document", "d", {"doc_field": [("ab", 1.5, None)]}).unsuitable() == 0


# ── 연결을 바꿔 끼운 시험 (서버 없이) ─────────────────────────────────────────────
class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def refusing(calls: list):
    def connect(url, timeout_s):
        calls.append(timeout_s)
        raise OSError(f"could not connect to {url} (password={SECRET})")       # 드라이버가 비밀번호를 섞어 말하는 경우
    return connect


def test_watch_goes_on_when_the_target_is_down_and_waits_before_reconnecting(world, tmp_path, monkeypatch, capsys):
    pipe, site = world["pipe"], world["site"]
    calls: list = []
    monkeypatch.setattr(core, "connect", refusing(calls))
    st = replace(world["st"], publish_url=URL, publish_connect_timeout_s=7.0, publish_retry_seconds=60.0,
                 excel_dir=tmp_path / "엑셀")
    (tmp_path / "엑셀").mkdir()
    clock = Clock()
    from minedocscan.export.auto import AutoExport

    pub = AutoPublish(st, clock=clock)
    assert pub.enabled
    worker = Worker(pipe, after={"excel": AutoExport(st, site), "publish": pub})
    out = worker.run_once()
    assert calls == [7.0] and out["excel"]["written"] and out["publish"]["error"] == "connect"
    line = format_round(out)
    assert "통합 DB: 싣지 못함 (connect" in line and SECRET not in line and URL not in line
    ops = OpsApp(pipe.con, site, st, "jp", worker=worker, excel=worker.after["excel"], publish=pub)
    home = json.dumps(ops.home_json(), ensure_ascii=False)
    assert SECRET not in home and "writer" not in home and "db.example.invalid:5432/site" in home
    assert ops.home_json()["publish"]["last_error"] == "connect" and ops.home_json()["publish"]["behind"] >= 1
    # retry_seconds 안의 바퀴는 연결 함수를 부르지 않는다 — 지나면 부른다
    clock.t += 59
    worker.run_once()
    assert calls == [7.0]
    clock.t += 2
    worker.run_once()
    assert calls == [7.0, 7.0]
    # watch --once 명령: 접수·처리·엑셀은 끝내고 종료 코드 0, 요약에 "싣지 못함" 한 줄
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", URL)
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(st.reviews))
    from minedocscan.cli import main

    common = ["--site", str(st.site), "--archive-root", str(st.archive_root), "--work-root", str(st.work_root)]
    assert main(["watch", "--once", *common]) == 0
    printed = capsys.readouterr()
    assert "통합 DB: 싣지 못함" in printed.out and SECRET not in printed.out + printed.err
    # publish 명령: 종료 코드 2 와 한 줄 (비밀번호 없이)
    assert main(["publish", *common]) == 2
    printed = capsys.readouterr()
    assert "닿지 못했습니다" in printed.err and SECRET not in printed.out + printed.err and "writer" not in printed.err
    assert main(["publish", "--check", *common]) == 2
    assert SECRET not in "".join(capsys.readouterr())
    # info: 호스트·DB·스키마만
    assert main(["info", "--json", *common]) == 0
    info = capsys.readouterr().out
    assert SECRET not in info and "writer" not in info
    assert json.loads(info)["publish"]["target"] == "db.example.invalid:5432/site"


def test_without_the_driver(world, monkeypatch, capsys):
    """psycopg 가 없으면 publish 는 한 줄과 종료 코드 2, watch 는 시작할 때 한 번 알리고 싣지 않는다."""
    from minedocscan.cli import _round_jobs, main

    monkeypatch.setitem(sys.modules, "psycopg", None)                 # import psycopg → ImportError
    st = replace(world["st"], publish_url=URL)
    jobs = _round_jobs(st, world["site"])
    assert not jobs["publish"].enabled and jobs["publish"].reason == "no_driver"
    assert "psycopg 가 없어" in capsys.readouterr().err
    assert jobs["publish"].after_round(world["pipe"].con, Touched(everything=True)) is None
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", URL)
    common = ["--site", str(st.site), "--work-root", str(st.work_root)]
    assert main(["publish", *common]) == 2
    err = capsys.readouterr().err
    assert "psycopg 가 없습니다" in err and SECRET not in err


def test_publish_url_only_from_the_environment(tmp_path, monkeypatch):
    from minedocscan.config import ConfigError, load_settings

    cfg = tmp_path / "minedocscan.toml"
    cfg.write_text(f'[publish]\nurl = "{URL}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_settings(cfg)
    assert SECRET not in str(e.value)
    cfg.write_text('[publish]\nschema = "site_a"\nretry_seconds = 10\nenabled = false\nlock_timeout_s = 2\n'
                   'statement_timeout_s = 30\n', encoding="utf-8")
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", URL)
    s = load_settings(cfg)
    assert s.publish_schema == "site_a" and s.publish_retry_seconds == 10 and not s.publish_on
    assert (s.publish_lock_timeout_s, s.publish_statement_timeout_s) == (2.0, 30.0)
    for bad in ("lock_timeout_s = 0", "lock_timeout_s = 601", "statement_timeout_s = 0.5", "statement_timeout_s = 3601",
                'lock_timeout_s = "five"'):
        cfg.write_text(f"[publish]\n{bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_settings(cfg)
    assert SECRET not in repr(s)
    cfg.write_text('[publish]\nschema = "Bad-Name"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_describe_and_scrub():
    assert core.describe_url(URL) == "db.example.invalid:5432/site"
    assert core.describe_url("postgresql://u@h/d") == "h/d"
    assert core.describe_url(f"postgresql://u:{SECRET}@h/d?sslmode=require") == "h/d"
    kw = f"host=db.example.invalid port=5433 dbname=site user=writer password={SECRET}"      # libpq 의 키워드 꼴
    assert core.describe_url(kw) == "db.example.invalid:5433/site" and SECRET not in core.describe_url(kw)
    assert SECRET not in core.describe_url(f"password='{SECRET}' dbname=x")
    text = core.scrub(f"failed: {URL} password={SECRET}", URL)
    assert SECRET not in text and "writer:" not in text
    # 비밀번호에 '/'·'?'·'#' 가 그대로 — 어디까지가 비밀인지 모른다. 키워드 꼴의 값에 '://' 가 있어도 키워드 꼴이다
    for bad in ("postgresql://writer:20/Ab9x@db.example.invalid/site", f"postgresql://w:/{SECRET}@h/d",
                f"postgresql://w:{SECRET}?x@h/d", f"postgres://w:{SECRET}#x@h/d", f"postgresql://md:4821/{SECRET}@h/d"):
        assert core.describe_url(bad) == "(읽을 수 없는 URL)", bad
        assert SECRET not in core.scrub(f"x {bad} y", bad) and "Ab9x" not in core.scrub(f"x {bad} y", bad)
    kw = f"host=127.0.0.1 port=1 password=Se://{SECRET} dbname=d"
    assert core.describe_url(kw) == "127.0.0.1:1/d"
    assert core.describe_url(f"host='a b' dbname='{SECRET} x'") == "?/?"       # 찍을 수 없는 글자의 이름은 ? 로


def test_info_and_the_home_do_not_show_odd_urls(world, monkeypatch, capsys):
    """설정이 이상한 꼴이어도(비밀번호에 '/', 키워드 꼴의 비밀번호에 '://') info·홈 JSON·연결 실패의 글에 비밀번호가 없다."""
    from minedocscan.cli import _worth_showing, main

    st = world["st"]
    common = ["--site", str(st.site), "--archive-root", str(st.archive_root), "--work-root", str(st.work_root)]
    monkeypatch.setattr(core, "connect", refusing([]))
    for url in (f"postgresql://writer:/{SECRET}@127.0.0.1:1/site", f"host=127.0.0.1 port=1 password=Se://{SECRET} dbname=site"):
        monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", url)
        assert main(["info", "--json", *common]) == 0
        assert main(["publish", *common]) == 2
        printed = "".join(capsys.readouterr())
        assert SECRET not in printed
        pub = AutoPublish(replace(st, publish_url=url))
        failed = pub.after_round(world["pipe"].con, Touched(everything=True))
        assert SECRET not in json.dumps(pub.status) + failed.message
    assert _worth_showing({"publish": {"replaced": {}, "removed": {}, "skipped": 1}})        # 건너뛴 범위만 있어도 알린다


@pytest.mark.postgres
@pytest.mark.skipif(not os.environ.get("MINEDOCSCAN_TEST_PG_URL"), reason="postgres 작업에서 (서버는 쓰지 않지만 드라이버의 시간 제한을 기다린다)")
def test_real_driver_to_a_closed_port_keeps_the_timeout_and_the_secret():
    """실제 드라이버로 닫힌 포트(127.0.0.1)에 붙으면 바로 실패하고, 받기만 하고 답하지 않는 서버에는 connect_timeout_s 에 실패한다
    (시간 제한이 드라이버에 닿는다 — 없으면 드라이버의 기본 130초). 우리가 내는 글에 비밀번호가 없다."""
    pytest.importorskip("psycopg")
    import socket
    import time

    from minedocscan.config import Settings

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                          # 닫힌 포트
    with pytest.raises(core.PublishError) as e:
        core.open_target(Settings(publish_url=f"postgresql://writer:{SECRET}@127.0.0.1:{port}/site", publish_connect_timeout_s=2.0))
    assert e.value.kind == "connect" and SECRET not in str(e.value) and "127.0.0.1" in str(e.value)
    silent = socket.socket()                                           # 연결은 받되(커널이 받는다) 답하지 않는다
    silent.bind(("127.0.0.1", 0))
    silent.listen(4)
    try:
        url = f"postgresql://writer:{SECRET}@127.0.0.1:{silent.getsockname()[1]}/site"
        t0 = time.monotonic()
        with pytest.raises(core.PublishError) as e:
            core.open_target(Settings(publish_url=url, publish_connect_timeout_s=2.0))
        took = time.monotonic() - t0
    finally:
        silent.close()
    assert 1.5 < took < 20, took
    assert e.value.kind == "connect" and SECRET not in str(e.value)


class FakeConn:
    """가짜 대상 연결: 시간 제한을 거는 문장(set_config)과 commit·rollback 을 적는다. fail: 그 밖의 문장에서 낼 예외."""

    def __init__(self, fail: Exception | None = None):
        self.log, self.sql, self.fail = [], [], fail

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, args=()):
                conn.sql.append((sql, args))
                if "set_config" in sql:
                    conn.log.append("limit")
                elif conn.fail is not None:
                    raise conn.fail

            def fetchone(self):
                return (1,)

            def fetchall(self):
                return []

        return Cur()

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    def close(self):
        self.log.append("close")


def test_a_conflict_in_a_dirty_publish_falls_back_to_a_full_sweep(monkeypatch):
    """더러운 범위만 실은 것이 키 충돌로 실패하면 되돌리고 같은 자리에서 전체 훑기로 다시 한다 (서버 없이 — 가짜 대상)."""
    class IntegrityError(Exception):
        sqlstate = "23505"

    conn = FakeConn()
    calls = []

    def fake_publish(con, target, full=True, **kw):
        calls.append(full)
        if not full:
            raise IntegrityError("duplicate key")
        return core.Result(full=True)

    monkeypatch.setattr(core, "publish", fake_publish)
    target = core.Target(conn, "minedocscan")
    r = core.run(None, None, full=False, documents={"d"}, dates={"2030-01-07"}, target=target, site="synthetic")
    # 시간 제한은 트랜잭션마다 건다 — 되돌린 뒤의 전체 훑기에도
    assert calls == [False, True] and r.fell_back and conn.log == ["limit", "rollback", "limit", "commit"]
    # 충돌이 아닌 실패는 되돌리고 알린다 (드라이버의 글 없이)
    def broken(con, target, full=True, **kw):
        raise RuntimeError(f"boom {SECRET}")

    monkeypatch.setattr(core, "publish", broken)
    with pytest.raises(core.PublishError) as e:
        core.run(None, None, full=False, target=target, site="synthetic")
    assert SECRET not in str(e.value) and conn.log[-1] == "rollback"


def test_any_failure_keeps_the_dirty_set(world, monkeypatch):
    """바퀴 끝의 싣기가 무엇으로 실패하든(작업 DB 를 읽다가 …) 건드린 것을 들고 있다가 다음에 같이 싣는다."""
    calls = []
    monkeypatch.setattr(core, "connect", lambda url, t: calls.append(1) or (_ for _ in ()).throw(OSError("down")))
    st = replace(world["st"], publish_url=URL, publish_retry_seconds=0.0)
    clock = Clock()
    pub = AutoPublish(st, clock=clock)

    def broken(self, con):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Touched, "all_dates", broken)
    r = pub.after_round(world["pipe"].con, Touched(documents={"d1"}, dates={"2030-01-07"}))
    assert r.as_dict()["error"] == "OperationalError" and "d1" in pub.held.documents and "2030-01-07" in pub.held.dates


# ── 연결한 뒤의 시간 제한 (PR #15 검토 1번) ──────────────────────────────────────────
class LockNotAvailable(Exception):
    sqlstate = "55P03"


class QueryCanceled(Exception):
    sqlstate = "57014"


class OperationalError(Exception):                    # 연결한 뒤에 끊겼다 (SQLSTATE 없음)
    sqlstate = None


def test_each_transaction_gets_the_lock_and_statement_timeouts():
    """트랜잭션마다 SET LOCAL(set_config(…, true))로 — 설정의 초를 밀리초로. 설정이 없으면(가짜 대상) Settings 의 기본값."""
    from minedocscan.config import Settings

    conn = FakeConn()
    core.Target(conn, "minedocscan").limit(2.5, 90)
    assert conn.sql == [("SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
                         ("2500ms", "90000ms"))]
    d = Settings()
    assert (d.publish_lock_timeout_s, d.publish_statement_timeout_s) == (5.0, 60.0)
    assert core._limits(None) == (5.0, 60.0)


@pytest.mark.parametrize("exc, kind", [(LockNotAvailable("canceling statement due to lock timeout"), "lock_timeout"),
                                       (QueryCanceled("canceling statement due to statement timeout"), "statement_timeout"),
                                       (OperationalError("canceling: server closed the connection"), "connection_lost")])
def test_timeouts_become_their_own_failure_and_roll_back(exc, kind, monkeypatch):
    """대상이 잠금·시간 제한으로 문장을 그만두게 하면 되돌리고 PublishError(종류 lock_timeout | statement_timeout) — 글에 비밀번호·드라이버의
    글이 없다. --rebuild 의 DROP 이 잠금을 기다린 것도 같다."""
    from minedocscan.config import Settings

    monkeypatch.setattr(core, "publish", lambda con, target, **kw: target.exists())     # 대상에 보내는 첫 문장에서
    st = Settings(publish_url=URL, publish_lock_timeout_s=3.0)
    conn = FakeConn(fail=exc)
    with pytest.raises(core.PublishError) as e:
        core.run(None, st, target=core.Target(conn, "minedocscan"), site="synthetic")
    assert e.value.kind == kind and conn.log[:2] == ["limit", "rollback"] and "commit" not in conn.log
    assert SECRET not in str(e.value) and "canceling" not in str(e.value)
    assert ("3초" in str(e.value)) == (kind == "lock_timeout")
    conn = FakeConn(fail=exc)
    with pytest.raises(core.PublishError) as e:
        core.run(None, st, target=core.Target(conn, "minedocscan"), rebuild=True, site="synthetic")
    assert e.value.kind == kind


def test_keepalive_goes_to_the_driver_unless_the_url_says_otherwise(monkeypatch):
    """실제 드라이버에 넘기는 것 (가짜 psycopg 로 — 드라이버 없이): connect_timeout, autocommit 꺼짐, TCP keepalive, 그리고 tcp_user_timeout =
    두 시간 제한 중 긴 것 + 30초 (서버의 시간 제한이 먼저 알리게 — 짧으면 멈춘 COPY 를 커널이 먼저 끊는다). URL 에 적은 값은 덮지 않는다."""
    import types

    from minedocscan.config import Settings

    got = []
    fake = types.ModuleType("psycopg")
    fake.connect = lambda url, **kw: got.append(kw) or "conn"
    conninfo = types.ModuleType("psycopg.conninfo")
    conninfo.conninfo_to_dict = lambda url: {"keepalives_idle": "60"} if "keepalives_idle" in url else {}
    fake.conninfo = conninfo
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", conninfo)
    assert core.psycopg_connect(URL, 4.6) == "conn"
    assert got[0] == {"connect_timeout": 5, "autocommit": False, **core.KEEPALIVE}
    assert core.KEEPALIVE == {"keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 2, "keepalives_count": 3}
    core.psycopg_connect(URL + "?keepalives_idle=60", 5)
    assert "keepalives_idle" not in got[1] and got[1]["keepalives_interval"] == 2
    core.open_target(Settings(publish_url=URL))                                       # 기본: 잠금 5, 문장 60 → 90초
    assert got[2]["tcp_user_timeout"] == 90_000 and got[2]["keepalives"] == 1
    core.open_target(Settings(publish_url=URL, publish_lock_timeout_s=600, publish_statement_timeout_s=20))
    assert got[3]["tcp_user_timeout"] == 630_000
    for lock_s, stmt_s in ((1, 1), (5, 3600), (600, 1)):
        assert core.user_timeout_ms(Settings(publish_lock_timeout_s=lock_s, publish_statement_timeout_s=stmt_s)) \
            >= (max(lock_s, stmt_s) + 30) * 1000


def test_a_stuck_target_is_visible_and_the_next_round_still_works(world, monkeypatch):
    """대상이 잠금을 기다리게 하다 그만두게 하면(가짜 연결이 LockNotAvailable): 그 바퀴는 끝나고 요약·홈에 그 종류가 보이고, 건드린 것을
    들고 있다. 싣는 동안 작업 상태는 publishing (홈: 몇 초째), 끝나면 idle. 다음 바퀴의 처리는 그대로 된다 (retry_seconds 안이라 연결하지 않는다)."""
    from minedocscan.intake import decisions as decs

    pipe, site = world["pipe"], world["site"]
    clock = Clock()
    seen = []
    worker = None

    def stuck(url, timeout_s):
        clock.t += 7                                                   # 대상이 기다리게 한 시간
        seen.append(dict(worker.status))
        ops_seen.append(OpsApp(pipe.con, site, st, "jp", worker=worker).home_json()["worker"])
        return FakeConn(fail=LockNotAvailable("lock timeout"))

    ops_seen: list = []
    monkeypatch.setattr(core, "connect", stuck)
    st = replace(world["st"], publish_url=URL, publish_retry_seconds=60.0, publish_sweep_minutes=0.0)
    pub = AutoPublish(st, clock=clock)
    worker = Worker(pipe, after={"publish": pub}, clock=clock)
    out = worker.run_once()
    assert out["publish"]["error"] == "lock_timeout" and worker.status == {"state": "idle"}
    assert seen[0]["state"] == "publishing" and ops_seen[0] == {"state": "publishing", "elapsed_s": 7}
    line = format_round(out)
    assert "통합 DB: 싣지 못함 (lock_timeout" in line and SECRET not in line
    assert pub.status["last_error"] == "lock_timeout" and pub.last_sweep is None
    assert out["publish"]["behind"] == pub.status["behind"] == 1                # 건드린 것이 없어도 밀린 전체 훑기 하나
    doc = world["ids"]["d_2030-01-08"]
    decs.save(pipe.con, st.decisions_path(site.root), [{"target": doc, "kind": "discard"}], "jp")
    clock.t += 55                                                      # 실패한 때부터 55초 (시작한 때부터는 62초)
    out = worker.run_once()
    assert out["processed"] == 1 and "publish" not in out and len(seen) == 1     # 처리는 되고, 다시 연결하지 않는다
    assert worker.status == {"state": "idle"} and pub.status["behind"] >= 1      # 기다리는 동안에도 밀린 것이 보인다
    clock.t += 6                                                       # 실패한 때부터 61초 — 다시 한다
    assert worker.run_once()["publish"]["error"] == "lock_timeout" and len(seen) == 2


# ── tasks/0009 4.1 사 — 작은 것들 ───────────────────────────────────────────────
def test_describe_url_refuses_a_url_missing_its_host():
    """'@호스트'를 빠뜨린 URL(postgresql://이름:비밀/db)은 읽을 수 없는 URL — 비밀번호를 호스트로 찍지 않는다. IPv6 는 그대로 읽는다.
    키워드 꼴의 host·dbname 에 ':' 이 있으면 ? (이름:비밀을 잘못 적었을 수 있다). 공백뿐이면 없는 것."""
    for bad in (f"postgresql://writer:{SECRET}/site", "postgresql://writer:/site", f"postgres://writer:{SECRET}"):
        assert core.describe_url(bad) == "(읽을 수 없는 URL)", bad
    v6 = core.describe_url("postgresql://[::1]:5432/db")
    assert v6 != "(읽을 수 없는 URL)" and "::1" in v6 and "5432" in v6 and v6.endswith("/db")
    assert core.describe_url("postgresql://db.example.invalid:5432/site") == "db.example.invalid:5432/site"   # 숫자 포트는 그대로
    assert core.describe_url("host=a:b dbname=c:d") == "?/?"
    assert core.describe_url(f"host=db.example.invalid dbname=writer:{SECRET}") == "db.example.invalid/?"
    for blank in ("", "   ", "\t\n", None):
        assert core.describe_url(blank) == "(없음)", repr(blank)


def test_a_blank_publish_url_is_no_url(tmp_path, monkeypatch):
    """공백뿐인 MINEDOCSCAN_PUBLISH_URL 은 없는 것 — 싣기는 꺼져 있다 (연결하지 않는다)."""
    from minedocscan.config import load_settings

    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", "   ")
    s = load_settings(tmp_path / "none.toml")
    assert s.publish_url is None and s.publish_on is False
    assert AutoPublish(s).reason == "off"


@pytest.fixture
def cmd_env(tmp_path, monkeypatch):
    """publish 명령을 서버 없이: 이름이 있는 사이트 팩, [site] name 이 없는 사이트 팩, 빈 작업 DB. 연결 함수는 부른 것을 적고 거절한다
    (설정이 틀리면 연결하기 전에 끝나야 한다)."""
    from minedocscan.store.db import open_db

    for k in ("MINEDOCSCAN_CONFIG", "MINEDOCSCAN_SITE", "MINEDOCSCAN_WORK_ROOT", "MINEDOCSCAN_DB_URL", "MINEDOCSCAN_ARCHIVE_ROOT",
              "MINEDOCSCAN_PUBLISH_URL", "MINEDOCSCAN_PUBLISH_SCHEMA", "MINEDOCSCAN_INBOX", "MINEDOCSCAN_EXCEL_DIR",
              "MINEDOCSCAN_REVIEWS", "MINEDOCSCAN_DAMAGED_PDF"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    named, unnamed = tmp_path / "site", tmp_path / "site-unnamed"
    for p, text in ((named, '[site]\nname = "synthetic"\n'), (unnamed, '[site]\ntitle = "Synthetic mine"\n')):
        p.mkdir()
        (p / "site.toml").write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    open_db(f"sqlite:///{(work / 'minedocscan.db').as_posix()}").close()
    calls: list = []
    monkeypatch.setattr(core, "connect", refusing(calls))
    return SimpleNamespace(root=tmp_path, named=named, unnamed=unnamed, work=work, calls=calls)


def test_publish_exit_code_is_2_when_the_setup_is_wrong(cmd_env, monkeypatch, capsys):
    """설정이 틀리면 publish 도 --check 도 종료 코드 2 (그 전에는 1 — --check 의 '다르다'와 같았다): URL 이 없다, 설정 파일이 깨졌다,
    작업 DB 가 없다·SQLite 가 아니다·DB 가 아닌 파일이다, [site] name 이 없다, 사이트 팩이 없다. 모두 연결하기 전에 — 비밀번호를 찍지
    않는다 (트레이스백도 없다)."""
    from minedocscan.cli import main

    e = cmd_env
    bad = e.root / "bad.toml"
    secret_cfg = e.root / "secret.toml"
    bad.write_text("[publish\nschema = 1\n", encoding="utf-8")
    secret_cfg.write_text(f'[publish]\nURL = "{URL}"\n', encoding="utf-8")
    ok = ["--site", str(e.named), "--work-root", str(e.work)]
    for check in ([], ["--check"]):
        capsys.readouterr()
        assert main(["publish", *check, *ok]) == 2, check                                  # URL 이 없다
        assert "MINEDOCSCAN_PUBLISH_URL" in capsys.readouterr().err
        monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", URL)
        assert main(["publish", *check, "--config", str(bad), *ok]) == 2, check           # 설정 파일이 깨졌다
        assert "설정 오류" in capsys.readouterr().err
        assert main(["publish", *check, "--config", str(secret_cfg), *ok]) == 2, check    # [publish] 에 URL
        err = capsys.readouterr().err
        assert "설정 오류" in err and SECRET not in err and "writer" not in err
        assert main(["publish", *check, "--site", str(e.named), "--work-root", str(e.root / "no-work")]) == 2, check
        assert "DB 가 없습니다" in capsys.readouterr().err                                  # 작업 DB 가 없다
        assert main(["publish", *check, "--site", str(e.unnamed), "--work-root", str(e.work)]) == 2, check
        err = capsys.readouterr().err
        assert "[site] name" in err and err.strip().count("\n") == 0                       # 한 줄
        assert main(["publish", *check, "--work-root", str(e.work)]) == 2, check            # 사이트 팩이 없다
        assert "--site" in capsys.readouterr().err
        monkeypatch.setenv("MINEDOCSCAN_DB_URL", f"postgresql://writer:{SECRET}@db.example.invalid/work")
        assert main(["publish", *check, *ok]) == 2, check                                  # 작업 DB 주소가 SQLite 가 아니다
        err = capsys.readouterr().err
        assert "작업 DB 를 열 수 없습니다" in err and SECRET not in err and "Traceback" not in err
        monkeypatch.delenv("MINEDOCSCAN_DB_URL")
        junk = e.root / f"junk{len(check)}"
        junk.mkdir(exist_ok=True)
        (junk / "minedocscan.db").write_bytes(b"not a database " * 100)
        assert main(["publish", *check, "--site", str(e.named), "--work-root", str(junk)]) == 2, check   # DB 가 아닌 파일
        assert "작업 DB 를 열 수 없습니다" in capsys.readouterr().err
        assert e.calls == [], check                                                          # 연결하지 않았다
        monkeypatch.delenv("MINEDOCSCAN_PUBLISH_URL")
    # 설정이 맞으면 연결한다 (위의 '연결하지 않았다'가 헛것이 아니다) — 닿지 못하면 2
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", URL)
    assert main(["publish", "--check", *ok]) == 2 and len(e.calls) == 1
    printed = "".join(capsys.readouterr())
    assert "닿지 못했습니다" in printed and SECRET not in printed


def test_a_rebuild_stopped_by_a_lock_says_run_it_again(cmd_env, monkeypatch, capsys):
    """--rebuild 가 잠금으로 그만두면 명령의 글은 '다시 실행하십시오' (바퀴 끝의 싣기의 '다음에 다시 합니다'가 아니다). 종료 코드 2."""
    from minedocscan.cli import main

    e = cmd_env
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", URL)
    seen = []

    def locked(con, settings, **kw):
        seen.append(kw)
        raise core._failure(LockNotAvailable("canceling statement due to lock timeout"), settings)

    monkeypatch.setattr(core, "run", locked)
    capsys.readouterr()
    assert main(["publish", "--rebuild", "--site", str(e.named), "--work-root", str(e.work)]) == 2
    err = capsys.readouterr().err
    assert seen[0]["rebuild"] is True and seen[0]["site"] == "synthetic"
    assert "다시 실행하십시오" in err and "다음에 다시 합니다" not in err and SECRET not in err


def test_a_rebuild_through_the_driver_stopped_by_a_lock_says_run_it_again(cmd_env, monkeypatch, capsys):
    """같은 것을 core.run 을 거쳐 (가짜 연결이 DROP 앞의 첫 문장에서 55P03): 종료 코드 2, '다시 실행하십시오', 되돌리고 닫는다."""
    from minedocscan.cli import main

    e = cmd_env
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", URL)
    conns = []

    def connect(url, timeout_s):
        conns.append(FakeConn(fail=LockNotAvailable("canceling statement due to lock timeout")))
        return conns[-1]

    monkeypatch.setattr(core, "connect", connect)
    capsys.readouterr()
    assert main(["publish", "--rebuild", "--site", str(e.named), "--work-root", str(e.work)]) == 2
    err = capsys.readouterr().err
    assert "다시 실행하십시오" in err and "다음에 다시 합니다" not in err and "canceling" not in err
    assert "commit" not in conns[0].log and conns[0].log[-1] == "close" and "rollback" in conns[0].log


@pytest.mark.parametrize("exc, kind", [(LockNotAvailable("lock timeout"), "lock_timeout"),
                                       (QueryCanceled("statement timeout"), "statement_timeout"),
                                       (OperationalError("server closed the connection"), "connection_lost")])
def test_for_command_turns_try_next_round_into_run_it_again(exc, kind):
    """시간 제한·끊김의 글: 바퀴 끝의 싣기는 '다음에 다시 합니다', 명령(for_command)은 '다시 실행하십시오'. 그 밖의 글은 그대로."""
    err = core._failure(exc, None)
    assert err.kind == kind and core.AGAIN in str(err)
    assert "다시 실행하십시오" in err.for_command() and core.AGAIN not in err.for_command()
    other = core.PublishError(core.OTHER_SITE, 2, "other_site")
    assert other.for_command() == str(other)


# ── tasks/0009 4.1 다 — [site] name 이 없으면 자동 싣기는 꺼진다 ────────────────────────
def test_without_a_site_name_auto_publish_is_off_and_says_so_once(tmp_path, capsys):
    """URL 이 있어도 사이트 팩에 [site] name 이 없으면 자동 싣기는 꺼지고(no_site_name) 시작할 때 한 줄 — 폴더 이름으로 대신하지 않는다.
    바퀴 끝의 싣기는 아무것도 하지 않는다 (연결하지 않는다). 엑셀 자동 내보내기도 같다."""
    from minedocscan.cli import _round_jobs
    from minedocscan.config import Settings
    from minedocscan.forms.sitepack import SitePack

    site = tmp_path / "synthetic"                                   # 폴더 이름이 그럴듯해도
    site.mkdir()
    (site / "site.toml").write_text('[site]\nname = "   "\ntitle = "Synthetic mine"\n', encoding="utf-8")
    (tmp_path / "엑셀").mkdir()
    st = Settings(site=site, work_root=tmp_path / "work", publish_url=URL, excel_dir=tmp_path / "엑셀")
    calls: list = []
    pub = AutoPublish(st, connect=refusing(calls))
    assert not pub.enabled and pub.reason == "no_site_name" and "[site] name" in pub.notice
    assert pub.status["enabled"] is False and pub.status["reason"] == "no_site_name"
    assert pub.after_round(None, Touched(everything=True)) is None and calls == []
    capsys.readouterr()
    jobs = _round_jobs(st, SitePack(site))
    err = capsys.readouterr().err
    assert jobs["publish"].reason == "no_site_name" and jobs["excel"].reason == "no_site_name"
    assert err.count(pub.notice) == 1 and err.count(jobs["excel"].notice) == 1 and len(err.strip().splitlines()) == 2
    assert SECRET not in err
    with pytest.raises(core.PublishError) as e:
        core.site_name(None, None)
    assert e.value.kind == "no_site_name" and e.value.code == 2
    with pytest.raises(core.PublishError):
        core.site_name(st)
    assert core.site_name(None, "synthetic") == "synthetic"


# ── tasks/0009 4.1 라 — 우리 쪽의 기다림의 상한 (서버 없이: 소켓 한 쌍) ─────────────────────────
class SocketConn:
    """가짜 대상 연결: 소켓 한 쌍의 한쪽(a)이 '서버로 가는 소켓'이다. 시간 제한을 거는 문장 밖의 문장은 a 에서 답 한 바이트를 기다리고,
    끝(b"")을 읽으면 드라이버처럼 OperationalError(SQLSTATE 없음). cancel: "hang"(취소도 돌아오지 않는다) | "works"(답이 온다).
    기다리는 것은 psycopg 의 윈도우 대기(waiting.wait_select)와 같이 0.1초마다 다시 거는 select — select 의 OSError 는 OperationalError
    (윈도우에서 감시 타이머가 깨우는 길이 그것이다 — 윈도우의 shutdown() 은 select·recv 를 깨우지 않는다)."""

    def __init__(self, cancel: str = "hang"):
        self.a, self.b = socket.socketpair()
        self.pgconn = SimpleNamespace(socket=self.a.fileno())
        self.cancel, self.cancels, self.log = cancel, 0, []
        self.release = threading.Event()

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, args=()):
                if "set_config" in sql:
                    return
                while True:
                    try:
                        r, _w, x = select.select([conn.a], [], [conn.a], 0.1)
                    except OSError:
                        raise OperationalError("connection socket closed") from None
                    if r or x:
                        break
                if conn.a.recv(1) == b"":
                    raise OperationalError("server closed the connection unexpectedly")

            def fetchone(self):
                return (1,)

        return Cur()

    def cancel_safe(self, timeout):
        self.cancels += 1
        if self.cancel == "hang":                                    # 멈춘 서버: 취소도 새 연결이라 기다린다
            self.release.wait(10)
            raise TimeoutError("cancel timed out")
        self.b.send(b"x")                                            # 취소가 들었다 — 문장이 돌아온다

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    def close(self):
        self.log.append("close")

    def dispose(self):
        self.release.set()
        self.a.close()
        self.b.close()


def _stalled(conn: SocketConn, dog: core.Watchdog) -> float:
    t0 = time.monotonic()
    with core.Target(conn, "minedocscan", dog).cur() as c:
        c.execute("SELECT pg_sleep(3600)")
    return time.monotonic() - t0


def test_watchdog_shuts_the_socket_when_the_cancel_hangs():
    """취소도 돌아오지 않으면(멈춘 서버) 감시 타이머가 소켓에 shutdown() — 기다리던 문장이 끝을 읽고 드라이버의 예외로 끝난다.
    기술자는 닫지 않는다 (libpq 의 것이다)."""
    conn = SocketConn("hang")
    dog = core.Watchdog(conn, limit_s=0.3, cancel_timeout_s=0.3, grace_s=0.1)
    try:
        t0 = time.monotonic()
        with pytest.raises(OperationalError):
            _stalled(conn, dog)
        took = time.monotonic() - t0
        assert dog.fired == "shutdown" and conn.cancels == 1
        assert 0.3 <= took < 0.3 + 0.3 + 3.0, took                       # 상한은 바쁜 CI 를 생각해 넉넉히
        conn.a.getsockname()                                         # 닫지 않았다 — 기술자는 그대로 열려 있다 (윈도우에서도 되는 검사)
        assert conn.b.recv(1) == b""                                 # 끊었다 (반대쪽이 끝을 읽는다)
    finally:
        conn.dispose()
        dog.close()


def test_psycopg_waits_with_select_on_windows():
    """감시 타이머의 윈도우 길(interrupt_waits)은 psycopg 가 윈도우에서 select 로 기다린다는 것에 기댄다 — 그것을 확인한다."""
    waiting = pytest.importorskip("psycopg.waiting")
    if sys.platform == "win32":
        assert waiting.wait is waiting.wait_select
    core.interrupt_waits(SimpleNamespace())                          # 끊긴 연결에도 조용히 (어느 OS 에서나)
    core.interrupt_waits(SimpleNamespace(pgconn=SimpleNamespace(socket=-1)))


def test_watchdog_cancel_that_works_does_not_shut_the_socket():
    """취소가 들으면(서버가 살아 있다) 문장이 돌아오고 소켓은 그대로 — fired 는 cancel 에서 멈춘다."""
    conn = SocketConn("works")
    dog = core.Watchdog(conn, limit_s=0.3, cancel_timeout_s=1.0, grace_s=1.0)   # 바쁜 기계에서도 취소 스레드를 기다리게 넉넉히
    try:
        took = _stalled(conn, dog)
        assert dog.fired == "cancel" and conn.cancels == 1 and 0.3 <= took < 3.5, took
        conn.b.send(b"y")                                            # 소켓이 살아 있다 — 끊지 않았다
        assert conn.a.recv(1) == b"y"
    finally:
        conn.dispose()
        dog.close()


def test_watchdog_does_not_fire_for_a_statement_that_returns_in_time():
    """제한 안에 돌아온 문장에는 아무것도 하지 않는다 — 문장이 끝나면 그 기한도 지운다 (다음 문장이 옛 기한에 걸리지 않는다)."""
    conn = SocketConn("hang")
    dog = core.Watchdog(conn, limit_s=0.3, cancel_timeout_s=0.3, grace_s=0.1)
    try:
        for _ in range(2):
            conn.b.send(b"x")                                        # 답이 이미 와 있다
            assert _stalled(conn, dog) < 0.3
        with dog._cv:                                                # 잠들지 않는다 — 돌아온 문장의 기한이 지워졌는지를 본다
            assert dog._deadline is None and dog._active is None
        assert dog.fired is None and conn.cancels == 0
        conn.b.send(b"y")
        assert conn.a.recv(1) == b"y"
    finally:
        conn.dispose()
        dog.close()
    core.shutdown_socket(SimpleNamespace())                          # 이미 끊긴 연결(pgconn 이 없다)에도 조용히


def test_a_stalled_target_ends_the_publish_as_connection_lost(monkeypatch):
    """core.run: 멈춘 문장을 감시 타이머가 끊으면 지금의 실패 경로 — PublishError(connection_lost), 되돌리고 커밋하지 않는다."""
    from minedocscan.config import Settings

    monkeypatch.setattr(core, "publish", lambda con, target, **kw: target.exists())     # 대상에 보내는 첫 문장에서 멈춘다
    conn = SocketConn("hang")
    dog = core.Watchdog(conn, limit_s=0.3, cancel_timeout_s=0.3, grace_s=0.1)
    try:
        t0 = time.monotonic()
        with pytest.raises(core.PublishError) as e:
            core.run(None, Settings(publish_url=URL), target=core.Target(conn, "minedocscan", dog), site="synthetic")
        assert time.monotonic() - t0 < 0.3 + 0.3 + 3.0
        assert e.value.kind == "connection_lost" and e.value.code == 2 and dog.fired == "shutdown"
        assert "rollback" in conn.log and "commit" not in conn.log
        assert SECRET not in str(e.value) and "unexpectedly" not in str(e.value)
    finally:
        conn.dispose()
        dog.close()


def test_open_target_watches_each_statement_for_the_statement_timeout_plus_a_margin(monkeypatch):
    """연결마다 감시 타이머 하나 — 상한은 statement_timeout_s + 30초 (서버가 살아 있으면 서버의 시간 제한이 먼저 끊는다)."""
    from minedocscan.config import Settings

    monkeypatch.setattr(core, "connect", lambda url, timeout_s: FakeConn())
    t = core.open_target(Settings(publish_url=URL, publish_statement_timeout_s=20))
    try:
        assert isinstance(t.watchdog, core.Watchdog) and t.watchdog.limit_s == 20 + core.WATCH_MARGIN_S == 50
        t.limit(5, 20)                                               # 감시 안에서 보내고, 돌아오면 아무것도 하지 않는다
        assert t.conn.log == ["limit"] and t.watchdog.fired is None
    finally:
        t.close()
    assert t.conn.log[-1] == "close"
