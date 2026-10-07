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
    assert first.created and first.full                                       # 시작할 때의 전체 훑기
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
        assert not r.fell_back and not r.full and r.checked < first.checked   # 더러운 범위만 (전체 훑기가 아니다)
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
    assert out.full and out.skipped == 1 and auto.status["skipped"] == 1 and _worth_showing({"publish": out.as_dict()})
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
