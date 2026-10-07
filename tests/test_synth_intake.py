"""합성 접수 시나리오 (tasks/0007 단계 6): synth --intake 묶음을 watch 한 바퀴와 truth.json 의 결정으로 끝까지 처리하면 문서·쪽의 상태가
truth 와 같고, 업무 테이블이 견줄 묶음(OUT/baseline — 같은 화소, 바로 선 것, 날짜 있는 이름, 빈 쪽 없이)을 run 한 것과 내용으로 같다.
네 날·서른 쪽 남짓을 두 번 돌린다 — 무거워서 -m slow (CI 의 slow 작업)에서 돈다. 렌더링·분류·정합은 저장해 둔 결과를 쓴다
(conftest.fast_imaging — 견줄 묶음의 쪽은 접수 묶음의 쪽과 화소까지 같다).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import fast_imaging
from minedocscan.config import Settings
from minedocscan.forms.sitepack import SitePack
from minedocscan.intake import decisions as decs
from minedocscan.intake.inbox import Inbox
from minedocscan.intake.worker import Worker
from minedocscan.pipeline import Pipeline
from minedocscan.tools.synth import generate
from test_reprocess import BUSINESS, no_null_dates

pytestmark = pytest.mark.slow


def content(con) -> dict:
    """업무 테이블의 내용 — 문서 ID·쪽 번호·필드 ID 가 들어간 열을 빼고 (두 묶음은 파일이 달라 ID 가 다르다)."""
    out = {}
    for t in BUSINESS:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")
                if not (r[1].endswith("_id") or r[1] in ("page_id", "other_page_id", "field_a", "field_b"))]
        out[t] = sorted((tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {t}")), key=repr)
    out["pages"] = sorted(tuple(r) for r in con.execute(
        "SELECT template_name, work_date, COUNT(*) FROM doc_page WHERE status = 'loaded' GROUP BY 1, 2"))
    return out


def test_intake_scenario_ends_like_truth_and_equals_the_baseline(tmp_path, monkeypatch):
    fast_imaging(monkeypatch)
    r = generate(tmp_path / "synth", days=4, seed=0, intake=True)
    truth = r.truth["intake"]
    site = SitePack(r.site)
    st = Settings(site=r.site, archive_root=tmp_path / "archive", work_root=tmp_path / "work",
                  reviews=tmp_path / "기록" / "reviews.jsonl", inbox=r.root / "inbox", save_aligned=False)
    st.archive_root.mkdir()
    pipe = Pipeline(st, site=site)
    worker = Worker(pipe, Inbox(st, pipe.con, settle_seconds=0, give_up_seconds=0))
    first = worker.run_once()
    undated = next(f["document_id"] for f in truth["files"] if f["kind"] == "undated")
    assert first["needs_date"] == [undated] and first["already"] == 1 and len(first["failed"]) == 1
    assert first.get("duplicates") == 2
    assert [p.name for p in (r.root / "inbox").iterdir() if p.is_file()] == []
    decs.save(pipe.con, st.decisions_path(site.root), truth["decisions"], "jp")
    worker.run_once()
    no_null_dates(pipe.con)
    con = pipe.con
    docs = dict(con.execute("SELECT document_id, status FROM doc_document").fetchall())
    assert docs == truth["expected"]["documents"]
    pages = dict(con.execute("SELECT page_id, status FROM doc_page").fetchall())
    assert pages == truth["expected"]["pages"]
    rot = {f"{f['document_id']}-p{p['page']}": p["rotation"] for f in truth["files"] for p in f["pages"]
           if p["status"] == "loaded"}
    assert dict(con.execute("SELECT page_id, rotation FROM doc_page WHERE status = 'loaded'").fetchall()) == rot
    assert len(list((r.root / "inbox" / "_already").iterdir())) == truth["expected"]["already"]
    # 견줄 묶음을 처음부터 run
    base = Pipeline(Settings(site=r.site, archive_root=r.root / "baseline", work_root=tmp_path / "base",
                             reviews=tmp_path / "다른" / "reviews.jsonl", save_aligned=False), site=site)
    base.run([r.root / "baseline"])
    a, b = content(con), content(base.con)
    for t in a:
        assert a[t] == b[t], t
    assert a["prod_haul"] and a["xcheck_haul"] and a["insp_daily"]


def test_intake_synth_is_byte_identical_for_the_same_seed(tmp_path):
    def digest(root: Path) -> dict:
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    a = generate(tmp_path / "a", days=4, seed=0, intake=True)
    b = generate(tmp_path / "b", days=4, seed=0, intake=True)
    assert digest(a.root) == digest(b.root)
    names = [f["name"] for f in json.loads(a.truth_path.read_text(encoding="utf-8"))["intake"]["files"]]
    assert names[0] == "scan_2030-01-07.pdf" and "scan0003.pdf" in names and len(names) == 8
