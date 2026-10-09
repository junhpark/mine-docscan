"""가상 양식 (V2)과 거친 손글씨 (tasks/0009 4.7, 단계 6): 새 양식이 **템플릿만으로** 적재·엑셀·통합 DB 까지 나간다.

- 파이프라인 코드는 V2 의 이름을 모른다 (src/ 에서 tools/ 밖에 양식 이름·fuel_/env_ 필드 이름이 없다).
- synth --v2-forms 를 oracle 로: V2 쪽이 전부 적재·정합, 인식기에 보낸 칸의 CER 0, 읽지 않는 칸(시각·소수)은 잉크 있음 + 검수 대기,
  일별 엑셀의 양식 시트에 읽는 칸의 값·읽지 않는 칸의 ? (정답을 검수로 넣으면 그 값), 통합 DB 의 doc_field 에 V2 의 행 (postgres).
- --rough 를 더해도 분류·정합 100 %, 인식기에 보낸 칸의 CER 0 (slow).
- 템플릿 도구(template init → add-region → 열·행을 적는다)로 만든 유류일지 템플릿: 칸이 4 px 안, oracle 의 결과가 같다 (slow).
값 유무의 정밀도·재현율(null)은 재는 것 — 시험 성적서와 보고에 (V2 의 수치로 인식률을 말하지 않는다).
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from minedocscan.config import Settings
from minedocscan.export.daily import daily_book
from minedocscan.export.labels import PENDING_MARK
from minedocscan.forms.formats import try_normalize
from minedocscan.forms.sitepack import SitePack
from minedocscan.forms.template import Template
from minedocscan.handlers.base import readable
from minedocscan.pipeline import Pipeline
from minedocscan.recognize import OracleRecognizer, load_answers_json
from minedocscan.review.store import review_from_field, save
from minedocscan.store.db import read_txn
from minedocscan.tools.synth import generate
from minedocscan.tools.synth_v2 import ENV_POINTS, FUEL_UNITS, T_ENV, T_FUEL, build_fuel_log

SRC = Path(__file__).resolve().parents[1] / "src" / "minedocscan"
V2 = (T_FUEL, T_ENV)
PG = os.environ.get("MINEDOCSCAN_TEST_PG_URL")


def test_the_pipeline_does_not_know_the_v2_names():
    """양식은 데이터다 (원칙 1): V2 의 고유 이름(양식 이름, fuel_·env_ 로 시작하는 이름)이 src/ 에서 tools/ 밖에 없다.
    (메타 키 operator·date.month, 형식 이름 integer·time·decimal 은 이미 쓰이는 말이라 보지 않는다.)"""
    pat = re.compile(r"\b(fuel|env)_[a-z]|fuel_log|env_log")
    hits = []
    for p in sorted(SRC.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".html", ".sql", ".yaml", ".toml", ".js", ".css") and "tools" not in p.relative_to(SRC).parts:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{p.relative_to(SRC)}:{i}")
    assert hits == []


def test_synth_cli_takes_the_two_options(tmp_path, capsys):
    import json

    from minedocscan.cli import main

    assert main(["synth", str(tmp_path / "합성"), "--days", "1", "--usage-only", "--v2-forms", "--rough", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    truth = json.loads(Path(out["truth"]).read_text(encoding="utf-8"))
    assert truth["rough"] is True and {t["template"] for t in truth["v2"]} == set(V2)
    assert {p.name for p in (Path(out["site"]) / "templates").iterdir()} >= set(V2)


def run_v2(root: Path, rough: bool = False, days: int = 1, site: Path | None = None, synth=None):
    syn = synth or generate(root / "synth", days=days, seed=0, v2_forms=True, rough=rough)
    st = Settings(site=site or syn.site, archive_root=syn.scans, work_root=root / "work", reviews=root / "기록" / "reviews.jsonl")
    answers = load_answers_json(syn.answers_path)
    pipe = Pipeline(st, recognizer=OracleRecognizer(answers))
    pipe.run([syn.scans])
    return SimpleNamespace(synth=syn, settings=st, pipe=pipe, answers=answers, root=root)


@pytest.fixture(scope="session")
def v2_run(tmp_path_factory):
    return run_v2(tmp_path_factory.mktemp("v2"))


def v2_fields(con) -> list:
    return con.execute(
        "SELECT f.*, d.source_name, p.page_no, p.template_name, p.work_date FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id WHERE p.template_name IN (?, ?) AND f.kind LIKE 'handwritten%'",
        V2).fetchall()


def scored(run) -> dict:
    """V2 칸: 인식기에 보낸(값 있음) 읽는 칸의 틀린 수, 값이 있는데 빈 칸으로 본 수(값 유무), 읽지 않는 칸의 상태."""
    out = {"sent": 0, "wrong": [], "missed": [], "unread": 0, "unread_bad": []}
    for r in v2_fields(run.pipe.con):
        key = (f"{r['source_name']}#{r['page_no']}", r["template_name"], r["region"], r["field_name"], r["row_key"] or "")
        truth = run.answers.get(key, "")
        if readable(SimpleNamespace(fmt=r["format"])):
            if r["has_value_raw"]:
                out["sent"] += 1
                if (try_normalize(r["format"], r["value_raw"] or "") or "") != (try_normalize(r["format"], truth) or ""):
                    out["wrong"].append(key)
            elif truth:
                out["missed"].append(key)
        elif truth:
            out["unread"] += 1
            if not (r["has_value_raw"] == 1 and r["status_raw"] == "pending"):
                out["unread_bad"].append(key)
    return out


def test_v2_pages_load_and_the_machine_read_cells_are_exact(v2_run):
    con = v2_run.pipe.con
    pages = con.execute("SELECT template_name, status, align_grid_err FROM doc_page WHERE template_name IN (?, ?)", V2).fetchall()
    assert sorted(r[0] for r in pages) == sorted(V2) and {r[1] for r in pages} == {"loaded"}
    assert all(r[2] is not None and r[2] <= 2.0 for r in pages)
    s = scored(v2_run)
    assert s["sent"] >= 20 and s["wrong"] == []                       # 인식기에 보낸 칸의 CER 0
    assert s["unread"] >= 5 and s["unread_bad"] == []                 # 주유 시각·pH: 잉크 있음 + 검수 대기
    # 값 유무를 놓친 칸은 표 밖의 월 "1"(세로획 하나 — 괘선 지우기가 지운다, CLAUDE.md)뿐이다 — 재현율에 나온다
    assert all(k[2] == "fields" and k[3].endswith("_date_month") for k in s["missed"]), s["missed"]
    printed = dict(con.execute("SELECT f.row_key, f.value_final FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                               "WHERE p.template_name = ? AND f.field_name = 'fuel_unit'", (T_FUEL,)).fetchall())
    assert printed == {u: u for u in FUEL_UNITS}                      # 인쇄된 값은 템플릿에서 (원칙 2)


def form_sheet(con, site, day: str, title: str) -> dict:
    """일별 파일의 양식 시트 → {(행 키, 열 표시 이름): 칸의 값}."""
    with read_txn(con):
        book = daily_book(con, site, day)
    sheet = next(s for s in book["sheets"] if s["name"] == title)
    head, out = None, {}
    for row in sheet["rows"]:
        vals = [c[0] for c in row]
        if vals and vals[0] == "행":
            head = vals
        elif head and vals and vals[0] not in (None, "쪽"):
            out.update({(vals[0], h): v for h, v in zip(head[1:], vals[1:], strict=False)})
    return out


def test_v2_forms_in_the_daily_excel_and_a_review_fills_the_unread_cell(v2_run, tmp_path):
    con, site = v2_run.pipe.con, v2_run.pipe.site
    day = v2_run.synth.truth["v2"][0]["date"]
    fuel = next(t for t in v2_run.synth.truth["v2"] if t["template"] == T_FUEL)
    sheet = form_sheet(con, site, day, "유류일지")
    for cell, text in fuel["cells"].items():
        unit, col = cell.split("|")
        shown = sheet[(unit, {"fuel_litres": "주유량(L)", "fuel_type": "유종", "fuel_by": "주유자", "fuel_time": "주유 시각"}[col])]
        if col == "fuel_time":
            assert shown == PENDING_MARK                              # 읽지 않는 칸은 ? — 값을 싣지 않는다
        elif col == "fuel_litres":
            assert shown == int(text)
        else:
            assert shown == text
    env = form_sheet(con, site, day, "환경일지")
    assert {k[0] for k in env} == {code for code, _n in ENV_POINTS}
    # 읽지 않는 칸에 정답을 검수로 넣으면 그 값
    unit, col = next(c.split("|") for c in fuel["cells"] if c.endswith("|fuel_time"))
    fid = con.execute("SELECT f.field_id FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id WHERE p.template_name = ? "
                      "AND f.field_name = 'fuel_time' AND f.row_key = ?", (T_FUEL, unit)).fetchone()[0]
    st = replace(v2_run.settings, reviews=tmp_path / "reviews.jsonl")
    from conftest import clone_db

    con2 = clone_db(con)
    save(con2, site, st, review_from_field(con2, fid, "value", fuel["cells"][f"{unit}|fuel_time"], "jp"))
    assert form_sheet(con2, site, day, "유류일지")[(unit, "주유 시각")] == fuel["cells"][f"{unit}|fuel_time"]


@pytest.mark.postgres
@pytest.mark.skipif(not PG, reason="MINEDOCSCAN_TEST_PG_URL 이 없다")
def test_v2_rows_reach_the_shared_db(v2_run):
    import psycopg

    from minedocscan.publish import core

    schema = f"v2_{uuid.uuid4().hex[:10]}"
    st = replace(v2_run.settings, publish_url=PG, publish_schema=schema)
    try:
        core.run(v2_run.pipe.con, st, site="synthetic")
        with psycopg.connect(PG) as c:
            n = c.execute(f'SELECT COUNT(*) FROM "{schema}".doc_field f JOIN "{schema}".doc_page p ON f.page_id = p.page_id '
                          "WHERE p.template_name IN (%s, %s)", V2).fetchone()[0]
        local = v2_run.pipe.con.execute("SELECT COUNT(*) FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                                        "WHERE p.template_name IN (?, ?)", V2).fetchone()[0]
        assert n == local > 0
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.slow
def test_rough_handwriting_still_classifies_aligns_and_reads(tmp_path):
    run = run_v2(tmp_path, rough=True, days=2)
    st = Counter_of(run.pipe.con)
    assert st == {"loaded": sum(len(info) for info in run.synth.truth["documents"].values())}
    s = scored(run)
    assert s["sent"] >= 40 and s["wrong"] == [] and s["unread_bad"] == []
    assert run.synth.truth["rough"] is True


def Counter_of(con) -> dict:
    from collections import Counter

    return dict(Counter(r[0] for r in con.execute("SELECT status FROM doc_page")))


@pytest.mark.slow
def test_a_template_made_with_the_template_tools_gives_the_same_cells(v2_run, tmp_path):
    """유류일지의 빈 기준 이미지에서 template init (분류 전용으로 둔다) → add-region → 열·행·필드를 적는다 (사람이 하는 일 — 시험이
    YAML 을 적는다). 칸이 생성기의 템플릿과 4 px 안이고, 같은 묶음을 oracle 로 돌린 결과가 같다 (docs/SITE_PACK.md 의 절차)."""
    import cv2

    from minedocscan.imaging.io import imwrite
    from minedocscan.tools.mktemplate import add_region, init_template

    blank, gen = build_fuel_log()
    img = tmp_path / "blank.png"
    imwrite(img, blank)
    site = tmp_path / "site"
    shutil.copytree(v2_run.synth.site, site)
    shutil.rmtree(site / "templates" / T_FUEL)
    path = init_template(img, T_FUEL, site / "templates")                # 쪽 전체의 괘선 — 분류 전용으로 둔다
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec["regions"] = []
    path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    g = gen["regions"][0]["grid"]
    roi = (g["xs"][0] - 10, g["ys"][0] - 10, g["xs"][-1] + 10, g["ys"][-1] + 10)
    add_region(path.parent, roi, "fuel_main", header_rows=1)
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    reg = spec["regions"][0]
    assert len(reg["columns"]) == len(gen["regions"][0]["columns"]) and len(reg["rows"]) == len(gen["regions"][0]["rows"])
    for c, want in zip(reg["columns"], gen["regions"][0]["columns"], strict=True):     # 사람이 적는 것: 열의 이름·종류·형식
        c.update({k: v for k, v in want.items() if k != "idx"})
    reg["rows"] = [dict(r) for r in gen["regions"][0]["rows"]]
    spec.update(handler="generic", fields=gen["fields"], display=gen["display"], title=gen["title"])
    path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tool, mine = Template(path), Template(v2_run.synth.site / "templates" / T_FUEL / "template.yaml")
    a = {(c.region, c.row_key, c.name): c.bbox for c in tool.cells()}
    b = {(c.region, c.row_key, c.name): c.bbox for c in mine.cells()}
    assert a.keys() == b.keys()
    assert max(abs(p - q) for k in a for p, q in zip(a[k], b[k], strict=True)) <= 4
    assert cv2.absdiff(tool.reference, mine.reference).max() == 0
    other = run_v2(tmp_path / "tool", site=site, synth=v2_run.synth)
    q = ("SELECT d.source_name, p.page_no, f.region, f.field_name, f.row_key, f.has_value_raw, f.value_raw, f.status_raw "
         "FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
         "WHERE p.template_name = ? ORDER BY 1, 2, 3, 4, 5")
    assert [tuple(r) for r in other.pipe.con.execute(q, (T_FUEL,))] == [tuple(r) for r in v2_run.pipe.con.execute(q, (T_FUEL,))]
    assert SitePack(site).templates[T_FUEL].name == T_FUEL
