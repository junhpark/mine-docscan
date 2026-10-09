"""통합 DB 로 싣기 — PostgreSQL 이 있는 환경에서 (tasks/0008 단계 5, 4.8). 표시 postgres, MINEDOCSCAN_TEST_PG_URL 이 없으면 건너뛴다.
시험마다 새 스키마를 만들고 끝나면 지운다 (서로 섞이지 않게). 합성 데이터만 — world(conftest: 운반·점검표·가동 일보 묶음).

불변식: 싣기가 끝나면 싣는 표마다 대상의 행 = 작업 DB 의 행 (싣는 열 전부, 행의 순서와 무관, 실수까지 정확히).
"""
from __future__ import annotations

import os
import uuid
from collections import Counter
from dataclasses import replace

import pytest

from minedocscan.intake import decisions as decs
from minedocscan.publish import core
from minedocscan.publish.auto import AutoPublish
from minedocscan.publish.scopes import columns
from minedocscan.review.store import review_from_field, save
from minedocscan.store.db import PUBLISH_SKIP_COLUMNS, PUBLISH_TABLES
from minedocscan.touched import Touched

PG = os.environ.get("MINEDOCSCAN_TEST_PG_URL")
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not PG, reason="MINEDOCSCAN_TEST_PG_URL 이 없다")]


@pytest.fixture
def pg(world):
    """이 시험만의 스키마 (끝나면 지운다 — 시험의 정리이므로 CASCADE)."""
    psycopg = pytest.importorskip("psycopg")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    yield replace(world["st"], publish_url=PG, publish_schema=schema, publish_retry_seconds=0.0)
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def remote(st, sql: str, args=()) -> list[tuple]:
    import psycopg

    with psycopg.connect(st.publish_url) as c:
        return [tuple(r) for r in c.execute(sql, args).fetchall()]


def assert_same(con, st, skip_docs: set[str] = frozenset()) -> None:
    """싣는 표마다 대상의 행 = 작업 DB 의 행 (skip_docs: 실을 수 없는 값이 있어 건너뛴 문서 — 그 문서의 행은 대상에 없다)."""
    pages = {r[0]: r[1] for r in con.execute("SELECT page_id, document_id FROM doc_page")}
    for t in PUBLISH_TABLES:
        cols = columns(con, t)
        local = Counter(tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {t}"))
        if skip_docs:
            key = "document_id" if t in ("doc_document", "doc_page") else ("page_id" if "page_id" in cols else None)
            if key:
                i = cols.index(key)
                local = Counter({r: n for r, n in local.items()
                                 if (r[i] if key == "document_id" else pages.get(r[i])) not in skip_docs})
        got = Counter(remote(st, f'SELECT {", ".join(f"{chr(34)}{c}{chr(34)}" for c in cols)} FROM "{st.publish_schema}"."{t}"'))
        assert got == local, (t, sum(got.values()), sum(local.values()))


def publish(world, st, **kw) -> core.Result:
    return core.run(world["pipe"].con, st, **kw)


# ── 불변식, 바뀐 것만 ──────────────────────────────────────────────────────────
def test_invariant_and_only_the_changed_scopes(world, pg):
    con = world["pipe"].con
    r = publish(world, pg)
    assert r.created and r.replaced["document"] == con.execute("SELECT COUNT(*) FROM doc_document").fetchone()[0]
    assert_same(con, pg)
    tables = {x[0] for x in remote(pg, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                                   (pg.publish_schema,))}
    assert tables == {*PUBLISH_TABLES, "pub_state", "pub_meta"}                 # 싣지 않는 표는 없다
    cols = {x[0] for x in remote(pg, "SELECT column_name FROM information_schema.columns WHERE table_schema = %s",
                                 (pg.publish_schema,))}
    assert not cols & set(PUBLISH_SKIP_COLUMNS)                                 # 싣지 않는 열도
    fks = remote(pg, "SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_schema = %s "
                     "AND constraint_type = 'FOREIGN KEY'", (pg.publish_schema,))
    assert fks == [(0,)]
    r = publish(world, pg)
    assert r.changed == 0 and r.rows == 0                                      # 바로 다시 — 갈아 끼운 범위 0
    r = publish(world, pg, full=False, documents={"0000000000000000"}, dates={"2031-01-01"})
    assert r.changed == 0                                                     # 대상에 없던 범위는 "지운 범위"로 세지 않는다
    # 운반 횟수 칸 하나를 검수하고 (더러운 범위만) 싣는다 — 그 문서와 그 날짜만
    fid, day, doc = con.execute("SELECT h.source_field_id, h.work_date, p.document_id FROM prod_haul h JOIN doc_page p "
                                "ON h.page_id = p.page_id WHERE h.source_role = 'log' AND h.review_status = 'pending' "
                                "ORDER BY h.haul_id LIMIT 1").fetchone()
    t = Touched()
    save(con, world["site"], world["st"], review_from_field(con, fid, "value", "8", "jp"), touched=t)
    r = publish(world, pg, full=False, documents=t.documents, dates=t.all_dates(con))
    # 그 문서와 그 날짜만 (날짜 범위는 교차검증이 바뀌었을 때만 — 행렬 쪽이 검수 대기면 그대로다)
    assert r.replaced["document"] == 1 and r.replaced["date"] <= 1 and r.replaced["whole"] == 0
    assert r.removed == {"document": 0, "date": 0, "whole": 0}
    assert remote(pg, f'SELECT key FROM "{pg.publish_schema}".pub_state WHERE kind = %s AND published_at = '
                      f'(SELECT MAX(published_at) FROM "{pg.publish_schema}".pub_state)', ("document",))
    assert_same(con, pg)
    assert publish(world, pg, check=True).changed == 0
    assert doc and day


def test_decisions_and_reprocessing_with_dirty_scopes_only(world, pg):
    """버리기·되살리기(문서·쪽), 날짜 바꾸기(다른 문서가 있는 날짜로), 다시 스캔 의심 쪽의 세 선택 — 그때마다 처리 → 더러운 범위만
    싣기 → 불변식. 점검표의 이기는 쪽이 바뀌는 경우(a 를 버리면 같은 날의 c 가 이긴다)와 일보의 자리가 다른 문서로 옮겨 가는 경우가 든다."""
    pipe, site, ids = world["pipe"], world["site"], world["ids"]
    con = pipe.con
    auto = AutoPublish(replace(pg, publish_sweep_minutes=0.0))
    first = auto.after_round(con, Touched())
    assert first.created and first.slices                                     # 시작할 때의 전체 훑기 (조각으로)
    whole = first.checked
    while auto.sweep.running:
        whole += auto.after_round(con, Touched()).checked
    path = world["st"].decisions_path(site.root)
    a, b, e = ids["a_2030-01-07"], ids["b_2030-01-07"], ids["e_2030-01-07"]
    winner = {r[0]: r[1] for r in con.execute("SELECT inspection_id, page_id FROM insp_daily")}
    held = [r[0] for r in con.execute("SELECT page_id FROM doc_page WHERE document_id = ? AND status = 'duplicate' ORDER BY page_id",
                                      (e,))]
    steps = [[{"target": a, "kind": "discard"}], [{"target": a, "kind": "restore"}], [{"target": f"{b}-p1", "kind": "discard"}],
             [{"target": b, "kind": "date", "value": "2030-01-08"}], [{"target": held[1], "kind": "keep"}],
             [{"target": held[0], "kind": "discard"}], [{"target": f"{a}-p2", "kind": "discard"}]]
    moved = False
    for items in steps:
        decs.save(con, path, items, "jp")
        pipe.process_pending()
        t, pipe.touched = pipe.touched, Touched()
        r = auto.after_round(con, t)
        assert r is not None and not isinstance(r, Exception) and not getattr(r, "kind", None), r
        assert not r.fell_back and not r.full and r.checked < whole           # 더러운 범위만 (전체 훑기가 아니다)
        assert_same(con, pg)
        now = {r_[0]: r_[1] for r_ in con.execute("SELECT inspection_id, page_id FROM insp_daily")}
        moved |= any(winner.get(k) and v.split("-p")[0] != winner[k].split("-p")[0] for k, v in now.items())
    assert moved                                                              # 이기는 쪽이 다른 문서로 옮겨 간 걸음이 있었다
    assert auto.fell_back == 0


def test_fresh_work_db_gives_zero_replaced_and_removes_gone_documents(world, pg, tmp_path):
    from test_reprocess import fresh_of

    st, site = world["st"], world["site"]
    publish(world, pg)
    gone = world["scans"] / "u3_2030-01-09.pdf"
    fresh_all = fresh_of(st, site, world["scans"], tmp_path, "fresh")
    r = core.run(fresh_all.con, pg)
    assert r.changed == 0                                                     # 내용이 같으면 지문이 같다
    gone.unlink()                                                             # 작업 DB 에서 빠진 파일
    fresh = fresh_of(st, site, world["scans"], tmp_path, "fresh2")
    r = core.run(fresh.con, pg)
    assert r.removed["document"] == 1 and r.replaced["document"] == 0
    assert_same(fresh.con, pg)


def test_one_transaction_and_unsuitable_values(world, pg):
    con = world["pipe"].con
    publish(world, pg)
    before = {t: Counter(remote(pg, f'SELECT * FROM "{pg.publish_schema}"."{t}"')) for t in (*PUBLISH_TABLES, "pub_state")}
    con.execute("UPDATE doc_field SET value_final = 'z' WHERE page_id LIKE ?", (world["ids"]["a_2030-01-07"] + "-%",))
    con.execute("UPDATE xcheck_haul SET log_trips = 77")
    con.commit()
    with pytest.raises(core.PublishError):
        publish(world, pg, fail_after=1)                                      # 넣는 가운데의 실패
    after = {t: Counter(remote(pg, f'SELECT * FROM "{pg.publish_schema}"."{t}"')) for t in (*PUBLISH_TABLES, "pub_state")}
    assert after == before                                                    # 싣기 전 그대로
    publish(world, pg)
    assert_same(con, pg)
    # NUL 문자가 든 값: 그 문서는 싣지 않고(옛 행도 지운다) 수로 알린다. 나머지는 실린다. 다음에도 "다르다"
    bad = world["ids"]["b_2030-01-07"]
    con.execute("UPDATE doc_field SET value_final = 'a' || char(0) || 'b' WHERE field_id = (SELECT MIN(field_id) FROM doc_field "
                "WHERE page_id LIKE ?)", (bad + "-%",))
    con.execute("UPDATE xcheck_haul SET log_trips = 78")
    con.commit()
    r = publish(world, pg)
    assert r.skipped == 1
    assert_same(con, pg, skip_docs={bad})
    assert remote(pg, f'SELECT COUNT(*) FROM "{pg.publish_schema}".doc_page WHERE document_id = %s', (bad,)) == [(0,)]
    assert publish(world, pg, check=True).replaced["document"] == 1
    # 바퀴 끝의 싣기: 건너뛴 범위의 수는 그 범위를 다시 견준 바퀴에서만 바뀐다 (다른 문서의 더러운 바퀴가 0 으로 덮지 않는다)
    from minedocscan.cli import _worth_showing

    auto = AutoPublish(replace(pg, publish_sweep_minutes=0.0))
    out = auto.after_round(con, Touched())
    assert out.slices and out.skipped == 1 and auto.status["skipped"] == 1 and _worth_showing({"publish": out.as_dict()})
    other = world["ids"]["d_2030-01-08"]
    assert not auto.after_round(con, Touched(documents={other})).full and auto.status["skipped"] == 1
    con.execute("UPDATE doc_field SET value_final = 'ab' WHERE value_final = 'a' || char(0) || 'b'")
    con.commit()
    assert not auto.after_round(con, Touched(documents={bad})).full and auto.status["skipped"] == 0
    assert_same(con, pg)


def test_versions_rebuild_and_views(world, pg):
    import psycopg

    publish(world, pg)
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'UPDATE "{pg.publish_schema}".pub_meta SET value = %s WHERE key = %s', ("999", "publish_version"))
    with pytest.raises(core.NeedRebuild) as e:
        publish(world, pg)
    assert "--rebuild" in str(e.value)
    before = remote(pg, f'SELECT COUNT(*) FROM "{pg.publish_schema}".doc_field')
    with pytest.raises(core.PublishError):                                # 다시 만들고 싣는 가운데 실패 — 한 트랜잭션이라 그 전 그대로
        publish(world, pg, rebuild=True, fail_after=1)
    assert remote(pg, f'SELECT COUNT(*) FROM "{pg.publish_schema}".doc_field') == before
    assert remote(pg, f'SELECT value FROM "{pg.publish_schema}".pub_meta WHERE key = %s', ("publish_version",)) == [("999",)]
    publish(world, pg, rebuild=True)
    assert_same(world["pipe"].con, pg)
    with psycopg.connect(PG, autocommit=True) as c:                           # 2단계의 뷰가 걸려 있다
        c.execute(f'CREATE VIEW "{pg.publish_schema}".v_haul AS SELECT * FROM "{pg.publish_schema}".prod_haul')
    with pytest.raises(core.PublishError) as e:
        publish(world, pg, rebuild=True)
    assert e.value.kind == "depends"
    assert remote(pg, f'SELECT COUNT(*) FROM "{pg.publish_schema}".prod_haul')[0][0] > 0       # 지우지 않았다


def test_a_schema_only_account_can_publish(world, pg):
    """관리자가 스키마를 만들어 준 전용 계정(DB 의 CREATE 권한 없이)으로도 싣는다 — 스키마가 있으면 CREATE SCHEMA 를 하지 않는다."""
    import psycopg

    role = f"r_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'pw-only-for-this-test'")       # DDL 은 자리표시자를 받지 않는다
        c.execute(f'CREATE SCHEMA "{pg.publish_schema}" AUTHORIZATION "{role}"')
        db = c.execute("SELECT current_database()").fetchone()[0]
        c.execute(f'REVOKE CREATE ON DATABASE "{db}" FROM PUBLIC')
    try:
        from urllib.parse import urlsplit

        u = urlsplit(PG)
        url = f"postgresql://{role}:pw-only-for-this-test@{u.hostname}:{u.port or 5432}{u.path}"
        r = core.run(world["pipe"].con, replace(pg, publish_url=url))
        assert r.created and r.replaced["document"]
        assert_same(world["pipe"].con, pg)
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{pg.publish_schema}" CASCADE')
            c.execute(f'DROP ROLE IF EXISTS "{role}"')


def test_check_writes_nothing_and_the_lock(world, pg, capsys, monkeypatch):
    from minedocscan.cli import main
    from minedocscan.pipeline.lock import PipelineLock

    st = world["st"]
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", PG)
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_SCHEMA", pg.publish_schema)
    common = ["--site", str(st.site), "--work-root", str(st.work_root)]
    lock = PipelineLock.for_settings(st).acquire()
    try:
        assert main(["publish", "--check", *common]) == 1                     # 표가 없다 — 다르다 (만들지 않는다)
        assert remote(pg, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s", (pg.publish_schema,)) == [(0,)]
        with pytest.raises(SystemExit) as e:
            main(["publish", *common])
        assert "이미 돌고" in str(e.value)
        with pytest.raises(SystemExit):
            main(["publish", "--rebuild", *common])
    finally:
        lock.release()
    assert main(["publish", *common]) == 0
    capsys.readouterr()
    state = remote(pg, f'SELECT * FROM "{pg.publish_schema}".pub_state ORDER BY 1, 2')
    lock = PipelineLock.for_settings(st).acquire()
    try:
        assert main(["publish", "--check", *common]) == 0                     # 잠금을 다른 프로세스가 잡고 있어도 돈다
    finally:
        lock.release()
    assert remote(pg, f'SELECT * FROM "{pg.publish_schema}".pub_state ORDER BY 1, 2') == state
    out = capsys.readouterr().out
    assert "postgres:" not in out and "@" not in out


# ── 흔들기에 싣기를 끼운 판 ──────────────────────────────────────────────────────
@pytest.mark.slow
@pytest.mark.parametrize("seed", [11, 12])
def test_reprocess_fuzz_with_dirty_publishing(world, pg, seed):
    """0007 의 불변식 흔들기에 싣기를 끼운 판: 걸음마다 더러운 범위만 싣고 대상 = 작업 DB 를 견준다 (전체 훑기 없이 — 넘어간 횟수를 센다).
    실패한 뒤 성공해 옛 failed 문서 행이 지워지는 걸음도 하나 넣는다 (대상에서도 지워진다)."""
    from test_review_store import reprocess_fuzz

    pipe = world["pipe"]
    con = pipe.con
    auto = AutoPublish(replace(pg, publish_sweep_minutes=0.0))
    auto.after_round(con, Touched())
    box = Touched()

    def step(n: int) -> None:
        t, pipe.touched = pipe.touched, Touched()
        t.add(box)
        box.__init__()
        everything = t.everything
        r = auto.after_round(con, t)
        assert r is None or hasattr(r, "replaced"), r
        assert r is None or not r.full or r.fell_back or everything, n         # 더러운 범위만 (넘어간 것은 따로 센다)
        assert_same(con, pg)

    reprocess_fuzz(world, steps=12, seed=seed, fresh=False, on_touched=box.add, after_step=step)
    # 깨진 파일 → failed 문서 → 고친 파일(다른 해시)이 같은 경로에서 성공하면 옛 failed 행이 지워진다 — 대상에서도
    f = world["scans"] / "u3_2030-01-09.pdf"
    good = f.read_bytes()
    f.write_bytes(good[: len(good) // 2])
    pipe.process_file(f)
    step(-1)
    failed = [r[0] for r in con.execute("SELECT document_id FROM doc_document WHERE status = 'failed'")]
    assert failed
    f.write_bytes(good + b"\n% changed\n")
    pipe.process_file(f)
    step(-2)
    assert not [r[0] for r in con.execute("SELECT document_id FROM doc_document WHERE status = 'failed'")]
    assert remote(pg, f'SELECT COUNT(*) FROM "{pg.publish_schema}".doc_document WHERE document_id = ANY(%s)', (failed,)) == [(0,)]
    print(f"seed {seed}: 전체 훑기로 넘어간 횟수 {auto.fell_back}")


# ── 연결한 뒤의 시간 제한 (PR #15 검토 1번) ──────────────────────────────────────────
class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def test_a_lock_on_the_target_ends_the_round_and_the_work_goes_on(world, pg):
    """다른 연결이 대상의 행을 쥐고 있으면(DB 도구의 커밋하지 않은 UPDATE) 그 문서를 갈아 끼우는 싣기는 lock_timeout_s 에 그만두고
    (대상은 그 전 그대로, 건드린 것은 들고 있다), 바퀴가 끝나 다음 바퀴의 처리는 그대로 된다. 놓으면 retry_seconds 뒤에 따라잡는다.
    싣는 동안 작업 상태는 publishing 이다 (홈이 "쉬는 중"이라고 하지 않는다)."""
    import threading
    import time

    psycopg = pytest.importorskip("psycopg")
    from minedocscan.intake.worker import Worker

    pipe, site, ids = world["pipe"], world["site"], world["ids"]
    con = pipe.con
    st = replace(pg, publish_lock_timeout_s=1.0, publish_retry_seconds=60.0, publish_sweep_minutes=0.0)
    clock = Clock()
    auto = AutoPublish(st, clock=clock)
    worker = Worker(pipe, after={"publish": auto})
    assert worker.run_once()["publish"]["created"]                           # 시작할 때의 전체 훑기 (조각으로 — 끝까지)
    while auto.sweep.running:
        worker.run_once()
    fid, doc = con.execute("SELECT h.source_field_id, p.document_id FROM prod_haul h JOIN doc_page p ON h.page_id = p.page_id "
                           "WHERE h.source_role = 'log' AND h.review_status = 'pending' ORDER BY h.haul_id LIMIT 1").fetchone()
    holder = psycopg.connect(PG)                                              # autocommit 꺼짐 — 커밋하지 않는다
    try:
        holder.execute(f'UPDATE "{st.publish_schema}".doc_document SET status = status WHERE document_id = %s', (doc,))
        state = f'SELECT kind, key, fingerprint FROM "{st.publish_schema}".pub_state ORDER BY 1, 2'
        before = remote(st, state)
        t = Touched()
        save(con, site, world["st"], review_from_field(con, fid, "value", "8", "jp"), touched=t)
        auto.mark(t)                                                          # 화면의 검수 저장처럼
        seen = []
        real = core.run

        def watching(*a, **kw):
            seen.append(dict(worker.status))
            return real(*a, **kw)

        core.run = watching
        out: dict = {}
        try:                                                                  # 시간 제한이 빠지면 멈춘다 — 실패로 끝나게 스레드에서
            th = threading.Thread(target=lambda: out.update(worker.run_once()), daemon=True)
            t0 = time.monotonic()
            th.start()
            th.join(30)
            took = time.monotonic() - t0
            if th.is_alive():
                holder.rollback()
                th.join(30)
                pytest.fail(f"잠금을 쥔 동안 바퀴가 30초 안에 끝나지 않았다 (작업 상태 {seen})")
        finally:
            core.run = real
        assert out["publish"]["error"] == "lock_timeout" and took < 20, (out, took)
        assert seen and seen[0]["state"] == "publishing" and worker.status == {"state": "idle"}
        assert auto.status["last_error"] == "lock_timeout" and auto.status["behind"] >= 1 and doc in auto.held.documents
        assert remote(st, state) == before                                      # 대상은 싣기 전 그대로
        # 잠금을 쥔 채로도 다음 바퀴는 처리한다 (retry_seconds 안이라 싣기는 연결하지 않는다)
        decs.save(con, world["st"].decisions_path(site.root), [{"target": ids["u3_2030-01-09"], "kind": "discard"}], "jp")
        t0 = time.monotonic()
        out = worker.run_once()
        assert out["processed"] == 1 and "publish" not in out and time.monotonic() - t0 < 20
    finally:
        holder.rollback()
        holder.close()
    clock.t += 61                                                             # 놓은 뒤 retry_seconds 가 지나면 따라잡는다
    out = worker.run_once()
    assert not out["publish"].get("error") and auto.status["last_error"] is None and not auto.held
    assert_same(con, st)


def test_a_slow_statement_is_cut_at_statement_timeout(world, pg):
    """대상의 문장 하나가 statement_timeout_s 를 넘으면(여기서는 지우기에 거는 3초짜리 트리거 — 2단계가 표에 무언가를 걸었다) 그 싣기를
    그만두고 종류 statement_timeout, 대상은 그 전 그대로. 트리거를 치우면 다음 싣기가 따라잡는다. 실제 드라이버에 keepalive 와 시간 제한이 간다."""
    import time

    psycopg = pytest.importorskip("psycopg")
    pipe, site = world["pipe"], world["site"]
    con = pipe.con
    st = replace(pg, publish_statement_timeout_s=1.0, publish_sweep_minutes=0.0)
    auto = AutoPublish(st)
    finish_cycle(auto, con)
    t = core.open_target(st)                                                  # 실제 드라이버에 간 것
    try:
        params = {i.keyword.decode(): (i.val or b"").decode() for i in t.conn.pgconn.info}
        assert {k: params[k] for k in core.KEEPALIVE} == {k: str(v) for k, v in core.KEEPALIVE.items()}
        assert params["tcp_user_timeout"] == str((5 + 30) * 1000)            # 두 시간 제한 중 긴 것(잠금 5초) + 30초
        t.limit(st.publish_lock_timeout_s, st.publish_statement_timeout_s)
        with t.cur() as c:
            c.execute("SHOW lock_timeout")
            lock = c.fetchone()[0]
            c.execute("SHOW statement_timeout")
            assert (lock, c.fetchone()[0]) == ("5s", "1s")
        t.conn.commit()                                                       # 되돌리기는 세션 SET 도 되돌린다 — 커밋한 뒤에 본다
        with t.cur() as c:
            c.execute("SHOW statement_timeout")
            assert c.fetchone()[0] != "1s"                                    # SET LOCAL — 트랜잭션이 끝나면 풀린다
        t.conn.rollback()
    finally:
        t.conn.close()
    sch = st.publish_schema
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'CREATE FUNCTION "{sch}".slow() RETURNS trigger LANGUAGE plpgsql AS '
                  "$$ BEGIN PERFORM pg_sleep(3); RETURN OLD; END $$")
        c.execute(f'CREATE TRIGGER slow BEFORE DELETE ON "{sch}".doc_document FOR EACH ROW EXECUTE FUNCTION "{sch}".slow()')
    fid = con.execute("SELECT source_field_id FROM prod_haul WHERE source_role = 'log' AND review_status = 'pending' "
                      "ORDER BY haul_id LIMIT 1").fetchone()[0]
    touched = Touched()
    save(con, site, world["st"], review_from_field(con, fid, "value", "8", "jp"), touched=touched)
    state = f'SELECT kind, key, fingerprint FROM "{sch}".pub_state ORDER BY 1, 2'
    before = remote(st, state)
    t0 = time.monotonic()
    r = auto.after_round(con, touched)
    assert r.kind == "statement_timeout" and time.monotonic() - t0 < 3, (r, time.monotonic() - t0)
    assert remote(st, state) == before and auto.status["last_error"] == "statement_timeout"
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'DROP TRIGGER slow ON "{sch}".doc_document')
    r = auto.after_round(con, Touched())                                      # 들고 있던 것을 싣는다 (retry_seconds = 0)
    assert r.replaced["document"] == 1 and auto.status["last_error"] is None
    assert_same(con, st)


# ── 사본의 주인 (tasks/0009 4.1 다) ─────────────────────────────────────────────────
def other_site_pack(world, tmp_path, name: str | None):
    """같은 사이트 팩을 복사하고 [site] name 만 바꾼 것 (name=None 이면 줄을 지운다)."""
    import re
    import shutil

    dst = tmp_path / "other-site"
    shutil.copytree(world["site"].root, dst)
    toml = dst / "site.toml"
    text = re.sub(r'(?m)^name\s*=.*$', f'name = "{name}"' if name else "", toml.read_text(encoding="utf-8"), count=1)
    toml.write_text(text, encoding="utf-8")
    return dst


def test_a_target_of_another_site_is_left_alone(world, pg, tmp_path, capsys, monkeypatch):
    """다른 사이트 팩의 작업 폴더로 같은 스키마에 싣기 → 종료 코드 2, 대상은 그대로 (행 수·지문). --check 도 2. 자동 싣기는 그 바퀴의
    실패 other_site. 글에 두 이름이 없다. --rebuild 로는 바꿔 실을 수 있다. 판이 1 인 대상(사이트 표식 이전)은 --rebuild 를 알린다."""
    import psycopg

    from minedocscan.cli import main

    con, st = world["pipe"].con, world["st"]
    publish(world, pg)
    sch = pg.publish_schema
    assert remote(pg, f'SELECT value FROM "{sch}".pub_meta WHERE key = %s', ("site",)) == [("synthetic",)]
    assert remote(pg, f'SELECT value FROM "{sch}".pub_meta WHERE key = %s', ("publish_version",)) == [("2",)]
    rows = {t: remote(pg, f'SELECT COUNT(*) FROM "{sch}"."{t}"')[0][0] for t in PUBLISH_TABLES}
    state = remote(pg, f'SELECT kind, key, fingerprint, published_at FROM "{sch}".pub_state ORDER BY 1, 2')
    other = other_site_pack(world, tmp_path, "site-two")
    with pytest.raises(core.PublishError) as e:
        core.run(con, replace(pg, site=other))
    assert e.value.kind == "other_site" and e.value.code == 2
    assert "site-two" not in str(e.value) and "synthetic" not in str(e.value) and "--rebuild" in str(e.value)
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", PG)
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_SCHEMA", sch)
    common = ["--site", str(other), "--work-root", str(st.work_root)]
    assert main(["publish", *common]) == 2
    assert main(["publish", "--check", *common]) == 2
    err = capsys.readouterr().err
    assert "다른 사이트 팩의 사본" in err and "site-two" not in err and "synthetic" not in err
    auto = AutoPublish(replace(pg, site=other, publish_sweep_minutes=0.0))
    failed = auto.after_round(con, Touched())
    assert failed.kind == "other_site" and auto.status["last_error"] == "other_site"
    assert {t: remote(pg, f'SELECT COUNT(*) FROM "{sch}"."{t}"')[0][0] for t in PUBLISH_TABLES} == rows       # 대상은 그대로
    assert remote(pg, f'SELECT kind, key, fingerprint, published_at FROM "{sch}".pub_state ORDER BY 1, 2') == state
    assert main(["publish", "--rebuild", *common]) == 0                     # 이 사이트로 바꿔 싣는다
    assert remote(pg, f'SELECT value FROM "{sch}".pub_meta WHERE key = %s', ("site",)) == [("site-two",)]
    assert_same(con, pg)
    with psycopg.connect(PG, autocommit=True) as c:                         # 사이트 표식 이전의 대상 (판 1, site 없음)
        c.execute(f'UPDATE "{sch}".pub_meta SET value = %s WHERE key = %s', ("1", "publish_version"))
        c.execute(f'DELETE FROM "{sch}".pub_meta WHERE key = %s', ("site",))
    with pytest.raises(core.NeedRebuild) as e:
        core.run(con, replace(pg, site=other))
    assert e.value.kind == "version" and "--rebuild" in str(e.value)


def test_rebuild_drops_only_what_this_program_made(world, pg):
    """pub_meta 가 없는데 이름이 같은 표가 있는 스키마: 싣기도 --rebuild 도 지우지 않고 알린다 (not_ours). 둘 다 없으면 그냥 만든다."""
    import psycopg

    sch = pg.publish_schema
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{sch}"')
        c.execute(f'CREATE TABLE "{sch}".doc_page (page_id TEXT PRIMARY KEY, note TEXT)')
        c.execute(f"INSERT INTO \"{sch}\".doc_page VALUES ('x', 'theirs')")
    for kw in ({}, {"rebuild": True}, {"check": True}):
        with pytest.raises(core.PublishError) as e:
            publish(world, pg, **kw)
        assert e.value.kind == "not_ours" and e.value.code == 2, kw
    assert remote(pg, f'SELECT page_id, note FROM "{sch}".doc_page') == [("x", "theirs")]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'DROP TABLE "{sch}".doc_page')
    r = publish(world, pg, rebuild=True)                                     # pub_meta 도 표도 없다 — 그냥 만든다
    assert r.full
    assert_same(world["pipe"].con, pg)


def test_a_dirty_round_after_the_tables_vanished_publishes_everything(world, pg):
    """더러운 범위만 싣는 바퀴에서 대상의 표가 없어 새로 만들었으면 새 조각 바퀴를 시작한다 (sweep_minutes = 0 — 다음 전체 훑기가
    없어도). 그 바퀴를 다 돌면 대상 = 작업 DB 이고 그때를 전체 훑기를 마친 것으로 센다."""
    import psycopg

    con = world["pipe"].con
    clock = Clock()
    auto = AutoPublish(replace(pg, publish_sweep_minutes=0.0), clock=clock)
    finish_cycle(auto, con)
    assert auto.after_round(con, Touched()) is None                     # 시작할 때 한 바퀴만
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{pg.publish_schema}" CASCADE')
    clock.t += 10
    r = auto.after_round(con, Touched(documents={world["ids"]["d_2030-01-08"]}))
    assert r.created and not r.full and auto.sweep.due()                # 더러운 범위만 실었다 — 다음 바퀴에 새 조각 바퀴
    clock.t += 10
    assert finish_cycle(auto, con) >= 2 and auto.last_sweep == clock.t
    assert_same(con, pg)


# ── 멈춰 버린 서버 (tasks/0009 4.1 라) ───────────────────────────────────────────────
class StallingRelay:
    """멈추는 TCP 중계: 127.0.0.1 의 빈 포트에서 받아 PostgreSQL 로 넘긴다. stall() 하면 소켓을 연 채 아무것도 넘기지 않는다 — 새 연결도
    받기만 한다 (서버의 커널은 살아 있는데 프로세스가 돌지 않는 것과 같다: 보낸 것은 받았다고 하고 keepalive 에도 답한다).
    release() 하면 다시 넘기고, 한쪽이 닫혔으면 다른 쪽도 닫는다 (서버의 옛 트랜잭션이 끝나 잠금을 놓는다)."""

    def __init__(self, url: str):
        import socket
        import threading
        from urllib.parse import urlsplit, urlunsplit

        u = urlsplit(url)
        if not u.hostname:
            pytest.skip("멈추는 중계는 postgresql://호스트[:포트]/DB 꼴의 URL 에서만")
        self.up = (u.hostname, u.port or 5432)
        self.ls = socket.socket()
        self.ls.bind(("127.0.0.1", 0))
        self.ls.listen(16)
        self.port = self.ls.getsockname()[1]
        userinfo = u.netloc.rpartition("@")[0]
        self.url = urlunsplit(u._replace(netloc=(userinfo + "@" if userinfo else "") + f"127.0.0.1:{self.port}"))
        self.go = threading.Event()
        self.go.set()
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def stall(self) -> None:
        self.go.clear()

    def release(self) -> None:
        self.go.set()

    def close(self) -> None:
        self.closed = True
        self.go.set()
        self.ls.close()

    def _accept(self) -> None:
        import threading

        while not self.closed:
            try:
                c, _ = self.ls.accept()
            except OSError:
                return
            threading.Thread(target=self._pair, args=(c,), daemon=True).start()

    def _pair(self, c) -> None:
        import socket
        import threading

        self.go.wait()                                   # 멈춘 동안 온 연결은 받기만 한다
        if self.closed:
            c.close()
            return
        u = socket.create_connection(self.up)
        threading.Thread(target=self._pump, args=(c, u), daemon=True).start()
        threading.Thread(target=self._pump, args=(u, c), daemon=True).start()

    def _pump(self, a, b) -> None:
        import socket

        try:
            while True:
                self.go.wait()
                data = a.recv(65536)
                self.go.wait()                           # 멈춘 동안 받은 것은 넘기지 않는다 (끝도)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()


def test_a_stalled_server_ends_the_round_within_a_bound(world, pg):
    """멈추는 중계를 사이에 두고 **답을 기다리는 동안** 멈추면(느린 트리거로 COPY 하나를 붙잡아 둔 뒤 — 서버는 문장을 끝내고 답을 보내지만
    중계가 넘기지 않는다: 서버의 시간 제한도, keepalive 도, tcp_user_timeout 도 작동하지 않는다) 감시 타이머가 statement_timeout_s + 30초에
    취소(cancel_safe 5초)를 보내고 소켓을 끊어 그 바퀴가 connection_lost 로 끝난다. 같은 작업의 다음 바퀴는 처리를 하고, 중계를 풀면
    따라잡아 대상 = 작업 DB (서버의 옛 트랜잭션이 남아 lock_timeout 에 걸리지 않는다). 감시 타이머가 없으면 바퀴가 끝나지 않아 실패한다."""
    import threading
    import time

    psycopg = pytest.importorskip("psycopg")
    from minedocscan.intake.worker import Worker

    pipe, site, ids = world["pipe"], world["site"], world["ids"]
    con = pipe.con
    relay = StallingRelay(PG)
    stmt_s = 2.0
    st = replace(pg, publish_url=relay.url, publish_lock_timeout_s=1.0, publish_statement_timeout_s=stmt_s,
                 publish_retry_seconds=60.0, publish_sweep_minutes=0.0, publish_connect_timeout_s=3.0)
    clock = Clock()
    auto = AutoPublish(st, clock=clock)
    worker = Worker(pipe, after={"publish": auto})
    try:
        assert worker.run_once()["publish"]["created"]                      # 중계를 거쳐 처음 싣기 (조각으로 — 끝까지)
        while auto.sweep.running:
            worker.run_once()
        sch = st.publish_schema
        with psycopg.connect(PG, autocommit=True) as c:                     # 문장 하나를 1.5초 붙잡는다 (문장의 시간 제한 2초 안)
            c.execute(f'CREATE FUNCTION "{sch}".hold() RETURNS trigger LANGUAGE plpgsql AS '
                      "$$ BEGIN PERFORM pg_sleep(1.5); RETURN NULL; END $$")
            c.execute(f'CREATE TRIGGER hold AFTER INSERT ON "{sch}".doc_document FOR EACH STATEMENT EXECUTE FUNCTION "{sch}".hold()')
        fid = con.execute("SELECT source_field_id FROM prod_haul WHERE source_role = 'log' AND review_status = 'pending' "
                          "ORDER BY haul_id LIMIT 1").fetchone()[0]
        t = Touched()
        save(con, site, world["st"], review_from_field(con, fid, "value", "8", "jp"), touched=t)
        auto.mark(t)
        out: dict = {}
        th = threading.Thread(target=lambda: out.update(worker.run_once()), daemon=True)
        t0 = time.monotonic()
        th.start()
        with psycopg.connect(PG, autocommit=True) as c:                     # 서버가 트리거에서 자는 동안 멈춘다
            for _ in range(400):
                if c.execute("SELECT COUNT(*) FROM pg_stat_activity WHERE wait_event = 'PgSleep'").fetchone()[0]:
                    break
                time.sleep(0.02)
            else:
                pytest.fail("트리거가 돌지 않았다")
        relay.stall()
        th.join(stmt_s + 35 + 8)
        took = time.monotonic() - t0
        if th.is_alive():
            relay.release()
            th.join(60)
            pytest.fail(f"멈춘 서버에서 바퀴가 {stmt_s + 35 + 8:g}초 안에 끝나지 않았다 — 감시 타이머가 없다")
        assert out["publish"]["error"] == "connection_lost", out
        assert stmt_s + 30 <= took < stmt_s + 35 + 5, took                  # 30초 넘게 기다린 뒤 (서버가 답할 틈) 취소 5초 안에
        assert auto.status["last_error"] == "connection_lost" and auto.held
        # 멈춘 채로 다음 바퀴: 처리는 된다 (retry_seconds 안이라 싣기는 연결하지 않는다)
        decs.save(con, world["st"].decisions_path(site.root), [{"target": ids["u3_2030-01-09"], "kind": "discard"}], "jp")
        t1 = time.monotonic()
        out = worker.run_once()
        assert out["processed"] == 1 and "publish" not in out and time.monotonic() - t1 < 20
        relay.release()                                                     # 풀면 클라이언트 쪽이 닫힌 것을 서버 쪽으로 넘긴다
        clock.t += 61
        out = worker.run_once()
        assert not out["publish"].get("error"), out                        # 옛 트랜잭션이 잠금을 쥐고 있지 않다 (트리거는 그대로 —
                                                                            # 1.5초씩 붙잡아도 문장의 시간 제한 안)
        assert auto.status["last_error"] is None and not auto.held
        assert_same(con, st)
    finally:
        relay.release()
        relay.close()


# ── 조각으로 나눈 전체 훑기 (tasks/0009 4.2 가) ───────────────────────────────────────
def two_months(world) -> None:
    pipe, site, st = world["pipe"], world["site"], world["st"]
    decs.save(pipe.con, st.decisions_path(site.root), [{"target": world["ids"]["d_2030-01-08"], "kind": "date",
                                                       "value": "2030-02-08"}], "jp")
    pipe.process_pending()
    pipe.touched = Touched()


def finish_cycle(auto, con, first=None, limit: int = 20) -> int:
    """바퀴 하나를 끝까지 (조각마다 작업 바퀴 하나). 돌려주는 값: 조각 수."""
    r = auto.after_round(con, first or Touched())
    assert not getattr(r, "kind", None), r
    n = 1
    while auto.sweep.running:
        r = auto.after_round(con, Touched())
        assert not getattr(r, "kind", None), r
        n += 1
        assert n < limit
    return n


def test_the_publish_sweep_goes_one_month_per_round(world, pg):
    """자동 싣기의 전체 훑기도 바퀴마다 달 하나, 마지막에 날짜 없는 조각. 처음 싣기도 조각으로 — 다 돌 때까지 대상은 일부만 있다.
    대상에만 있는 달의 것도 지워진다. 다 돌면 대상 = 작업 DB."""
    import psycopg

    con = world["pipe"].con
    two_months(world)
    clock = Clock()
    auto = AutoPublish(replace(pg, publish_sweep_minutes=0.0), clock=clock)
    r = auto.after_round(con, Touched())                                # 첫 조각 2030-01 (표를 만들었다)
    assert r.created and r.slices == ["2030-01", "2030-02", "-"]
    assert auto.status["sweep"] == {"done": 1, "total": 3, "first": True}
    sch = pg.publish_schema
    days = {x[0] for x in remote(pg, f'SELECT DISTINCT work_date FROM "{sch}".doc_page')}
    assert days and all(d.startswith("2030-01") for d in days)          # 아직 일부만
    auto.after_round(con, Touched())
    auto.after_round(con, Touched())
    assert auto.status["sweep"] is None and auto.status["last_sweep_at"] and auto.last_sweep == clock.t
    assert_same(con, pg)
    assert auto.after_round(con, Touched()) is None                     # sweep_minutes = 0 — 시작할 때 한 바퀴만
    with psycopg.connect(PG, autocommit=True) as c:                         # 대상에만 있는 달 (다른 프로그램이 남긴 것이 아니라 옛 싣기의 것)
        c.execute(f'INSERT INTO "{sch}".pub_state (kind, key, fingerprint, published_at) VALUES (%s, %s, %s, %s)',
                  ("date", "2029-12-31", "0" * 64, "x"))
        c.execute(f'INSERT INTO "{sch}".eq_assignment_obs (work_date, slot, matched_by, header_mismatch) VALUES (%s, %s, %s, %s)',
                  ("2029-12-31", "T09", "vehicle", 0))
    again = AutoPublish(replace(pg, publish_sweep_minutes=0.0), clock=clock)
    assert finish_cycle(again, con) == 4                                # 2029-12, 2030-01, 2030-02, 날짜 없음
    assert_same(con, pg)
    assert remote(pg, f'SELECT COUNT(*) FROM "{sch}".pub_state WHERE key = %s', ("2029-12-31",)) == [(0,)]


def test_watch_once_publishes_everything_in_its_one_round(world, pg):
    """watch --once 의 싣기는 할 때가 된 전체 훑기를 그 바퀴에 한 번에 (명령 publish 처럼 — 다음 바퀴가 없다)."""
    con = world["pipe"].con
    two_months(world)
    auto = AutoPublish(replace(pg, publish_sweep_minutes=0.0), once=True)
    r = auto.after_round(con, Touched())
    assert r.full and r.created and auto.status["sweep"] is None and auto.status["last_sweep_at"]
    assert_same(con, pg)


def test_the_sweep_finds_target_only_keys_under_any_collation(world, pg):
    """대상의 정렬 규칙이 C 가 아니어도(ICU en-US — '-'·'~' 같은 문장 부호를 먼저 무시한다: '2030-01-31' < '2030-01-~' 가 거짓) 달의 조각이
    대상에만 있는 날짜를 찾는다. 'YYYY-MM' 처럼 끝이 잘린 날짜 키는 날짜 없는 조각이 찾는다 — 한 바퀴 뒤 대상 = 작업 DB."""
    import psycopg

    con = world["pipe"].con
    clock = Clock()
    auto = AutoPublish(replace(pg, publish_sweep_minutes=0.0), clock=clock)
    finish_cycle(auto, con)
    sch = pg.publish_schema
    with psycopg.connect(PG, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_collation WHERE collname = 'en-US-x-icu'").fetchone():
            pytest.skip("ICU 정렬 규칙이 없다")
        c.execute(f'ALTER TABLE "{sch}".pub_state ALTER COLUMN key TYPE TEXT COLLATE "en-US-x-icu"')
        c.execute(f'ALTER TABLE "{sch}".doc_page ALTER COLUMN work_date TYPE TEXT COLLATE "en-US-x-icu"')
        c.execute(f'ALTER TABLE "{sch}".eq_assignment_obs ALTER COLUMN work_date TYPE TEXT COLLATE "en-US-x-icu"')
        for key in ("2030-01-31", "2030-01"):
            c.execute(f'INSERT INTO "{sch}".pub_state (kind, key, fingerprint, published_at) VALUES (%s, %s, %s, %s)',
                      ("date", key, "0" * 64, "x"))
            c.execute(f'INSERT INTO "{sch}".eq_assignment_obs (work_date, slot, matched_by, header_mismatch) VALUES (%s, %s, %s, %s)',
                      (key, "T09", "vehicle", 0))
    again = AutoPublish(replace(pg, publish_sweep_minutes=0.0), clock=clock)
    assert finish_cycle(again, con) == 2                                # 2030-01, 날짜 없음
    assert_same(con, pg)
    assert remote(pg, f'SELECT COUNT(*) FROM "{sch}".pub_state WHERE kind = %s AND key IN (%s, %s)',
                  ("date", "2030-01-31", "2030-01")) == [(0,)]


@pytest.mark.slow
@pytest.mark.parametrize("seed", [21, 22])
def test_a_sliced_publish_sweep_equals_the_work_db(world, pg, seed):
    """조각으로 돈 한 바퀴 = 전체 훑기 (싣기): 걸음마다 검수(건드린 것을 넘기지 않는다 — 다른 프로세스처럼)·결정(처리가 건드린 것은
    넘긴다)을 넣고 바퀴 하나를 끝까지 돌린 뒤 대상 = 작업 DB."""
    from test_review_store import reprocess_fuzz

    pipe = world["pipe"]
    con = pipe.con
    two_months(world)
    clock = Clock()
    auto = AutoPublish(replace(pg, publish_sweep_minutes=1.0), clock=clock)
    parts = [finish_cycle(auto, con)]
    assert_same(con, pg)

    def step(n):
        t, pipe.touched = pipe.touched, Touched()
        clock.t += 61.0
        parts.append(finish_cycle(auto, con, t))
        assert_same(con, pg)

    reprocess_fuzz(world, steps=8, seed=seed, fresh=False, on_touched=lambda t: None, after_step=step)
    assert max(parts) >= 3 and auto.fell_back == 0, parts
