"""끝에서 끝까지 (tasks/0008 단계 7): 합성 접수 묶음(synth --intake --usage-logs --print-layers --display-names)을 엑셀 폴더와
통합 DB 를 준 watch --once 로 → doc date → watch --once → doc discard → watch --once. 끝난 뒤 일별·월별 파일이 DB 와 같고, 대상의
표가 작업 DB 와 같고(PostgreSQL 이 있으면), 가린 쪽 그림이 나온다. 그 엑셀이 견줄 묶음(baseline)을 run 한 DB 로 내보낸 엑셀과
내용이 같다 — 문서를 가리키는 칸(출처, 쪽 ID, 필드 ID, 쪽의 머리)과 쪽의 순서는 빼고 (0007 단계 6 과 같은 비교).
무겁다 — -m slow (PostgreSQL 이 있으면 -m "slow and postgres" 판도).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import uuid
from collections import Counter
from pathlib import Path

import pytest

from conftest import fast_imaging
from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.export import labels as L
from minedocscan.export.writer import DAILY_RE, MONTHLY_RE, export_excel
from minedocscan.export.xlsx import read_values
from minedocscan.forms.sitepack import SitePack
from minedocscan.pipeline import Pipeline
from minedocscan.store.db import open_db
from minedocscan.tools.synth import generate
from test_export_auto import same_as_db

PG = os.environ.get("MINEDOCSCAN_TEST_PG_URL")
DROP = {L.SOURCE, L.FIELD_ID, "비교한 쪽"}             # 문서를 가리키는 열


def content(out: Path) -> dict:
    """엑셀 폴더의 내용 — 요약 시트를 빼고, 문서를 가리키는 칸을 빼고, 쪽의 순서와 무관하게 (여러 벌의 모음으로)."""
    got = {}
    for p in sorted(out.rglob("*.xlsx")):
        rel = p.relative_to(out).as_posix()
        assert DAILY_RE.match(rel) or MONTHLY_RE.match(rel), rel
        for name, rows in read_values(p).items():
            if name == L.SUMMARY:
                continue
            if any(r and r[0] == L.PAGE_HEAD for r in rows):            # 양식 시트: 쪽 블록의 모음 (머리 줄 빼고)
                blocks, cur = [], None
                for r in rows:
                    if r and r[0] == L.PAGE_HEAD:
                        cur = [(r[3], r[5])]                           # 양식의 이름과 검수 대기 칸의 수만
                        blocks.append(cur)
                    elif cur is not None:
                        cur.append(tuple(r))
                got[(rel, name)] = Counter(tuple(b) for b in blocks)
            elif name == L.SHEETS["haul_table"]:                         # 운반 표: 자리 블록은 그대로, 자리 미정 블록은 출처 없이 모음으로
                top, head = rows[0], rows[1]
                starts = [i for i, v in enumerate(top) if v] + [len(head)]
                table = []
                for r in rows[2:]:
                    r = list(r) + [None] * (len(head) - len(r))
                    fixed, loose = [tuple(r[:2])], []
                    for a, z in zip(starts, starts[1:], strict=False):
                        part = tuple(v for j, v in enumerate(r[a:z]) if head[a + j] != L.SOURCE)
                        (loose if str(top[a]).startswith(L.UNRESOLVED_SLOT) else fixed).append((top[a] if not
                                                                                               str(top[a]).startswith(L.UNRESOLVED_SLOT)
                                                                                               else None, part))
                    table.append((tuple(fixed), tuple(sorted(loose, key=repr))))
                got[(rel, name)] = (tuple(top[:2]), tuple(sorted(table, key=repr)))
            else:                                                       # 업무 시트·긴 표: 문서를 가리키는 열을 빼고 행의 모음
                head = rows[0] if rows else []
                keep = [i for i, h in enumerate(head) if h not in DROP]
                got[(rel, name)] = (tuple(head[i] for i in keep),
                                    Counter(tuple(r[i] if i < len(r) else None for i in keep) for r in rows[1:]))
    return got


def cli(*args) -> dict | int:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main([*args])
    text = buf.getvalue()
    return json.loads(text) if "--json" in args else code


@pytest.mark.slow
@pytest.mark.parametrize("with_pg", [pytest.param(True, marks=[pytest.mark.postgres,
                                                               pytest.mark.skipif(not PG, reason="MINEDOCSCAN_TEST_PG_URL 이 없다")]),
                                     False], ids=["publish", "excel-only"])
def test_from_the_scanner_folder_to_the_files_and_the_target(tmp_path, monkeypatch, with_pg):
    fast_imaging(monkeypatch)
    r = generate(tmp_path / "synth", days=4, seed=0, intake=True, usage_logs=True, print_layers=True, display_names=True)
    truth = r.truth["intake"]
    out, archive, work = tmp_path / "엑셀 폴더", tmp_path / "archive", tmp_path / "work"
    out.mkdir()
    archive.mkdir()
    monkeypatch.setenv("MINEDOCSCAN_EXCEL_DIR", str(out))
    monkeypatch.setenv("MINEDOCSCAN_INBOX", str(r.root / "inbox"))
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(tmp_path / "기록" / "reviews.jsonl"))
    schema = f"e2e_{uuid.uuid4().hex[:10]}"
    if with_pg:
        monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", PG)
        monkeypatch.setenv("MINEDOCSCAN_PUBLISH_SCHEMA", schema)
    common = ["--site", str(r.site), "--archive-root", str(archive), "--work-root", str(work)]
    watch = ["watch", "--once", "--json", "--settle-seconds", "0", "--give-up-seconds", "0", *common]
    try:
        first = cli(*watch)["watch"]
        undated = next(f["document_id"] for f in truth["files"] if f["kind"] == "undated")
        assert first["needs_date"] == [undated] and first["excel"]["written"]
        if with_pg:
            assert first["publish"]["created"]
        date = next(d for d in truth["decisions"] if d["kind"] == "date")
        assert cli("doc", "date", date["target"], date["value"], "--reviewer", "jp", *common) == 0
        second = cli(*watch)["watch"]
        assert second["processed"] >= 1 and second["excel"]["written"]
        for d in (d for d in truth["decisions"] if d["kind"] == "discard"):
            assert cli("doc", "discard", d["target"], "--reviewer", "jp", *common) == 0
        third = cli(*watch)["watch"]
        assert third["excel"]["written"]
        con = open_db(f"sqlite:///{work / 'minedocscan.db'}")
        site = SitePack(r.site)
        n = same_as_db(con, site, out)                                    # 일별·월별 파일 = DB (되읽어서)
        assert n >= 5
        if with_pg:
            from dataclasses import replace

            from test_publish_pg import assert_same

            assert cli("publish", "--check", *common) == 0
            assert_same(con, replace(Settings(), publish_url=PG, publish_schema=schema))
        # 가린 쪽 그림 (날짜마다)
        masked = tmp_path / "가린 그림"
        days = sorted(x[0] for x in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE status = 'loaded'"))
        for d in days:
            assert cli("export", "masked-pages", str(masked), "--date", d, *common) == 0
        assert len(list(masked.iterdir())) == con.execute("SELECT COUNT(*) FROM doc_page WHERE status = 'loaded'").fetchone()[0]
        # 견줄 묶음을 처음부터 run 한 DB 로 내보낸 엑셀과 내용이 같다
        base = Pipeline(Settings(site=r.site, archive_root=r.root / "baseline", work_root=tmp_path / "base",
                                 reviews=tmp_path / "다른" / "reviews.jsonl", save_aligned=False), site=site)
        base.run([r.root / "baseline"])
        bout = tmp_path / "견줄 엑셀"
        bout.mkdir()
        export_excel(base.con, site, bout, full=True)
        a, b = content(out), content(bout)
        assert sorted(a) == sorted(b)
        for k in a:
            assert a[k] == b[k], k
        con.close()
    finally:
        if with_pg:
            import psycopg

            with psycopg.connect(PG, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
