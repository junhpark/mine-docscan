"""통합 DB 로 싣기 — 서버 없이 되는 것 (tasks/0008 단계 5, 4.8): 대상의 DDL(schema.sql 에서), 범위와 지문(순수 함수),
연결 함수를 바꿔 끼운 시험(닿지 않는 서버 — 비밀번호가 어디에도 찍히지 않는다, 다시 연결하는 간격), 드라이버가 없을 때.
PostgreSQL 이 있어야 하는 것은 test_publish_pg.py (-m postgres).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import replace

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
    cfg.write_text('[publish]\nschema = "site_a"\nretry_seconds = 10\nenabled = false\n', encoding="utf-8")
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", URL)
    s = load_settings(cfg)
    assert s.publish_schema == "site_a" and s.publish_retry_seconds == 10 and not s.publish_on
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


def test_real_driver_to_a_closed_port_keeps_the_timeout_and_the_secret():
    """실제 드라이버로 닫힌 포트(127.0.0.1)에 붙으면 connect_timeout_s 안에 실패하고, 우리가 내는 글에 비밀번호가 없다."""
    pytest.importorskip("psycopg")
    import socket
    import time

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                          # 닫힌 포트
    url = f"postgresql://writer:{SECRET}@127.0.0.1:{port}/site"
    from minedocscan.config import Settings

    st = Settings(publish_url=url, publish_connect_timeout_s=2.0)
    t0 = time.monotonic()
    with pytest.raises(core.PublishError) as e:
        core.open_target(st)
    assert time.monotonic() - t0 < 10
    assert e.value.kind == "connect" and SECRET not in str(e.value) and "127.0.0.1" in str(e.value)


def test_a_conflict_in_a_dirty_publish_falls_back_to_a_full_sweep(monkeypatch):
    """더러운 범위만 실은 것이 키 충돌로 실패하면 되돌리고 같은 자리에서 전체 훑기로 다시 한다 (서버 없이 — 가짜 대상)."""
    class IntegrityError(Exception):
        sqlstate = "23505"

    class Conn:
        def __init__(self):
            self.log = []

        def commit(self):
            self.log.append("commit")

        def rollback(self):
            self.log.append("rollback")

        def close(self):
            self.log.append("close")

    conn = Conn()
    calls = []

    def fake_publish(con, target, full=True, **kw):
        calls.append(full)
        if not full:
            raise IntegrityError("duplicate key")
        return core.Result(full=True)

    monkeypatch.setattr(core, "publish", fake_publish)
    target = core.Target(conn, "minedocscan")
    r = core.run(None, None, full=False, documents={"d"}, dates={"2030-01-07"}, target=target)
    assert calls == [False, True] and r.fell_back and conn.log == ["rollback", "commit"]
    # 충돌이 아닌 실패는 되돌리고 알린다 (드라이버의 글 없이)
    def broken(con, target, full=True, **kw):
        raise RuntimeError(f"boom {SECRET}")

    monkeypatch.setattr(core, "publish", broken)
    with pytest.raises(core.PublishError) as e:
        core.run(None, None, full=False, target=target)
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
