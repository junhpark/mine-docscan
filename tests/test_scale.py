"""규모 (tasks/0009 4.2 마): 조각 하나가 읽는 행 수가 날짜 수에 비례하지 않는다 — 30일·60일로 복제한 작업 DB 에서 같은 달(2031-01)의
조각이 읽는 행이 같고, 최대 메모리(tracemalloc)가 늘지 않는다. 벽시계가 아니라 읽은 행 수와 tracemalloc 으로 (CI 의 벽시계는 흔들린다).
252일 규모의 수치는 시험이 아니라 재는 것이다 — scripts/bigdb.py, docs/test-report/scale-*.json.

복제는 scripts/bigdb.py 의 replicate (세션의 작업 DB — world 의 묶음에서, 밀도 1). 합성 데이터만. -m slow (싣기는 slow and postgres).
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import tracemalloc
from dataclasses import replace
from pathlib import Path

import pytest

from minedocscan.export.writer import export_excel
from minedocscan.forms.sitepack import SitePack
from minedocscan.store.db import open_db

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]
MONTH = "2031-01"                                    # 복제의 첫 달 (평일 2031-01-06 부터 — 30일·60일 모두 같은 20일)


def bigdb():
    spec = importlib.util.spec_from_file_location("bigdb", ROOT / "scripts" / "bigdb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Counting:
    """읽은 행을 센다 — 연결의 row_factory 가 행마다 불린다 (sqlite3.Row 를 그대로 돌려준다)."""

    def __init__(self, con: sqlite3.Connection):
        self.n = 0
        con.row_factory = self

    def __call__(self, cursor, row):
        self.n += 1
        return sqlite3.Row(cursor, row)


def measure(fn) -> tuple[int, object]:
    """(tracemalloc 의 최대 바이트, 결과) — 읽은 행은 Counting 이 센다."""
    tracemalloc.start()
    try:
        out = fn()
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak, out


@pytest.fixture(scope="module")
def replicas(bundles, world_db, tmp_path_factory) -> dict[int, Path]:
    """world 의 작업 DB 를 평일 30일·60일로 복제한 작업 폴더 (밀도 1)."""
    mod = bigdb()
    site_dir = bundles["site"]
    out = {}
    for days in (30, 60):
        work = tmp_path_factory.mktemp(f"scale{days}") / "work"
        src = sqlite3.connect(":memory:")
        world_db.backup(src)
        mod.replicate(src, site_dir, work, days=days, density=1)
        src.close()
        out[days] = work
    return out


def test_an_excel_slice_reads_the_same_rows_at_30_and_60_days(replicas, bundles, tmp_path):
    site = SitePack(bundles["site"])
    got = {}
    for days, work in replicas.items():
        con = open_db(f"sqlite:///{(work / 'minedocscan.db').as_posix()}")
        out = tmp_path / f"엑셀{days}"
        out.mkdir()
        export_excel(con, site, out, full=True)                         # 처음 훑기 — 그 뒤의 바뀐 것 없는 조각을 잰다
        rows = Counting(con)
        peak, r = measure(lambda con=con, out=out: export_excel(con, site, out, slices=[MONTH]))
        assert r.written == [] and r.unchanged == 21, r.as_dict()       # 그 달의 20일 + 월별 하나
        got[days] = (rows.n, peak)
        con.close()
    (n30, p30), (n60, p60) = got[30], got[60]
    assert n30 == n60 and n30 > 0, got                                  # 날짜 수에 비례하지 않는다
    assert p60 <= p30 * 1.2 + 1_000_000, got


@pytest.mark.postgres
@pytest.mark.skipif(not os.environ.get("MINEDOCSCAN_TEST_PG_URL"), reason="MINEDOCSCAN_TEST_PG_URL 이 없다")
def test_a_publish_slice_reads_the_same_rows_at_30_and_60_days(replicas, bundles, world):
    import uuid

    import psycopg

    from minedocscan.publish import core

    pg = os.environ["MINEDOCSCAN_TEST_PG_URL"]
    got = {}
    for days, work in replicas.items():
        schema = f"t_{uuid.uuid4().hex[:12]}"
        st = replace(world["st"], site=bundles["site"], work_root=work, publish_url=pg, publish_schema=schema)
        con = open_db(st.resolved_db_url)
        try:
            core.run(con, st, full=True)                                # 처음 싣기 — 그 뒤의 바뀐 것 없는 조각을 잰다
            rows = Counting(con)
            peak, r = measure(lambda con=con, st=st: core.run(con, st, full=False, part=MONTH))
            assert r.changed == 0 and r.checked > 20, r.as_dict()
            got[days] = (rows.n, peak)
        finally:
            con.close()
            with psycopg.connect(pg, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    (n30, p30), (n60, p60) = got[30], got[60]
    assert n30 == n60 and n30 > 0, got
    assert p60 <= p30 * 1.2 + 1_000_000, got
