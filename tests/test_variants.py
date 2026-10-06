"""같은 날 섞여 쓰이는 판 (tasks/0006 단계 4, 4.6): concurrent, 분류의 묶음, 판마다 정합해 고르기, template variant, 합성 판 B.

두 판을 둔 실행은 세션의 합성 가동 일보(usage_synth·usage_run — 운행일보가 판 A·B 로 섞였다)를 같이 쓴다 — 새 파이프라인 실행이
없다. 판 A 만 있을 때의 실패는 판 B 쪽을 판 A 에만 정합해 함수 단위로 잰다 (파이프라인이 판 A 하나로 하는 일과 같다).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import yaml

from conftest import clone_db
from minedocscan.cli import main
from minedocscan.evaluate.fields import evaluate_fields
from minedocscan.forms.classify import FormClassifier
from minedocscan.forms.sitepack import SitePack
from minedocscan.forms.template import Template, TemplateError
from minedocscan.imaging.align import align_to_template
from minedocscan.imaging.io import imwrite, load_page
from minedocscan.pipeline.runner import choose_variant
from minedocscan.recognize import OracleRecognizer
from minedocscan.recognize.base import CellContext
from minedocscan.report import build_report, format_report, list_pages
from minedocscan.tools import synth_usage
from minedocscan.tools.synth import T_INSP, T_LOG, generate, scan_effect, write_site_pack
from minedocscan.tools.synth_usage import T_LOADER, T_USAGE, T_USAGE_B, USAGE_LOGS
from minedocscan.tools.variant import VariantError, _pair, make_variant

GROUP = f"{T_USAGE} / {T_USAGE_B}"


def _pages(con) -> dict[str, dict]:
    return {r["source"]: dict(r) for r in con.execute(
        "SELECT d.source_name || '#' || p.page_no AS source, p.*, d.source_path FROM doc_page p "
        "JOIN doc_document d ON p.document_id = d.document_id")}


# ── 사이트 팩: concurrent 의 규칙 ─────────────────────────────────────────────────
@pytest.fixture()
def pack(tmp_path) -> Path:
    """판 A·B 가 있는 합성 사이트 팩 (스캔 없이 — 템플릿만)."""
    return write_site_pack(tmp_path / "site", usage=True, usage_variants=True)


def _edit(site: Path, name: str, fn) -> None:
    p = site / "templates" / name / "template.yaml"
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    fn(spec)
    p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_concurrent_variants_load_as_one_group(pack):
    site = SitePack(pack)
    a, b = site.templates[T_USAGE], site.templates[T_USAGE_B]
    assert (a.family, a.concurrent, b.family, b.concurrent) == (T_USAGE, True, T_USAGE, True)
    assert site.concurrent_groups("2030-01-07") == site.concurrent_groups(None) == {T_USAGE: [T_USAGE, T_USAGE_B]}
    assert site.answer_key(T_USAGE) == site.answer_key(T_USAGE_B) == T_USAGE
    assert site.answer_key(T_LOADER) == T_LOADER and site.answer_key("no_such_form") == "no_such_form"
    # 판 B 의 유효 기간이 끝난 뒤에는 판 A 하나 — 묶지 않는다 (판이 하나뿐인 날은 정합도 한 번)
    _edit(pack, T_USAGE_B, lambda s: s.update(valid_to="2030-01-07"))
    site = SitePack(pack)
    assert site.concurrent_groups("2030-01-07") == {T_USAGE: [T_USAGE, T_USAGE_B]}
    assert site.concurrent_groups("2030-01-08") == {}
    # 유효 기간이 겹치지 않으면 키가 달라도 된다 (동시 판이 아니다 — 날짜로 가리는 개정판, tasks/0002)
    _edit(pack, T_USAGE_B, lambda s: s["regions"][1]["columns"][2].update(format="decimal"))
    _edit(pack, T_USAGE_B, lambda s: s.update(valid_to="2030-01-06"))
    _edit(pack, T_USAGE, lambda s: s.update(valid_from="2030-01-07"))
    site = SitePack(pack)                                                    # 키가 다른 채로 읽힌다
    assert site.templates[T_USAGE_B].region("meter")["columns"][2]["format"] == "decimal"
    assert site.concurrent_groups("2030-01-06") == site.concurrent_groups("2030-01-07") == {}


@pytest.mark.parametrize("case, fn, needle", [
    ("one side", lambda site: _edit(site, T_USAGE_B, lambda s: s.pop("concurrent")), "concurrent: true"),
    ("not bool", lambda site: _edit(site, T_USAGE_B, lambda s: s.update(concurrent="yes")), "true/false"),
    ("no family", lambda site: [_edit(site, n, lambda s: s.pop("family")) for n in USAGE_LOGS], "family 가 있을 때만"),
    ("table name", lambda site: _edit(site, T_USAGE_B, lambda s: s["regions"][1].update(name="hours")), "표 이름"),
    ("role", lambda site: _edit(site, T_USAGE_B, lambda s: [s["regions"][0].pop("role")] + [r.pop("subtotal", None)
                                                                                         for r in s["regions"][0]["rows"]]),
     "표 work 의 role"),
    ("column", lambda site: _edit(site, T_USAGE_B, lambda s: s["regions"][1]["columns"][2].update(format="decimal")),
     "표 meter 의 열"),
    ("row", lambda site: _edit(site, T_USAGE_B, lambda s: s["regions"][0]["rows"][0].update(key="SECRET-ROW-KEY")),
     "표 work 의 행"),
    ("field", lambda site: _edit(site, T_USAGE_B, lambda s: s["fields"][1].update(meta_key="SECRET-KEY")), "필드"),
    ("row meta", lambda site: _edit(site, T_USAGE_B, lambda s: s["regions"][0]["rows"][0].update(no="SECRET-9")),
     "표 work 의 행"),
    ("column meta", lambda site: _edit(site, T_USAGE_B, lambda s: s["regions"][1]["columns"][0].update(shift="SECRET-N")),
     "표 meter 의 열"),
    ("header_rows", lambda site: _edit(site, T_USAGE_B, lambda s: s["regions"][0].update(header_rows=0)),
     "표 work 의 header_rows"),
    ("handler_options", lambda site: _edit(site, T_USAGE_B, lambda s: s.update(handler_options={"SECRET-OPT": 1})),
     "handler_options"),
    # `template variant` 를 돌린 바로 뒤: 새 판에만 family·concurrent 가 있고 기존 판(이름 = 계열)은 그대로
    ("lone", lambda site: _edit(site, T_USAGE, lambda s: [s.pop("family"), s.pop("concurrent")]), "하나뿐"),
    ("lone, no namesake", lambda site: [_edit(site, T_USAGE, lambda s: [s.pop("family"), s.pop("concurrent")]),
                                        _edit(site, T_USAGE_B, lambda s: s.update(family="fam_x"))], "다른 판의 template.yaml"),
    # 이름이 계열인 판이 이미 그 계열(날짜로 가린 개정판)이면 빠진 것은 concurrent 한 줄이다
    ("lone, namesake in family", lambda site: [_edit(site, T_USAGE, lambda s: [s.pop("concurrent"), s.update(valid_to="2030-01-07")]),
                                               _edit(site, T_USAGE_B, lambda s: s.update(valid_from="2030-01-08"))], "하나뿐"),
])
def test_site_pack_refuses_broken_concurrent_variants(pack, case, fn, needle):
    """concurrent 가 한쪽에만, 계열에 하나뿐, family 없이, true/false 가 아닌 값, 동시 판끼리 키(handler_options, 표 이름·role·
    header_rows·열·행 — 메타까지, 필드)가 다르면 사이트 팩을 읽을 때 오류다. 키가 다를 때의 메시지는 두 판의 이름과 처음 다른 항목의
    종류만 — 행 키·값은 찍지 않는다. 계열에 하나뿐이면 그 판과 계열, 두 줄을 적을 판(계열과 이름이 같은 템플릿이 있으면 그것)을 말한다."""
    fn(pack)
    with pytest.raises(TemplateError) as e:
        SitePack(pack)
    msg = str(e.value)
    assert needle in msg, (case, msg)
    assert "SECRET" not in msg and "\n" not in msg
    if case in ("table name", "role", "column", "row", "field", "row meta", "column meta", "header_rows", "handler_options"):
        assert T_USAGE in msg and T_USAGE_B in msg and "키가 다릅니다" in msg
    if case == "one side":
        assert "겹칩니다" in msg
    if case == "lone":
        assert f"{T_USAGE_B} 하나뿐" in msg and f"{T_USAGE} 의 template.yaml" in msg
        assert f"family: {T_USAGE}" in msg and "concurrent: true" in msg
    if case == "lone, no namesake":
        assert f"{T_USAGE_B} 하나뿐" in msg and "family: fam_x" in msg
    if case == "lone, namesake in family":
        assert f"{T_USAGE} 의 template.yaml 에 concurrent: true 를" in msg and "family:" not in msg


def test_header_rows_left_out_is_zero_for_the_key_check(pack):
    """동시 판의 키 비교에서 적지 않은 header_rows 는 0 이다 (Template 이 읽는 기본값과 같다) — 0 과 생략은 같은 키다."""
    _edit(pack, T_USAGE, lambda s: s["regions"][1].update(header_rows=0))
    _edit(pack, T_USAGE_B, lambda s: s["regions"][1].pop("header_rows"))
    site = SitePack(pack)
    assert site.concurrent_groups(None) == {T_USAGE: [T_USAGE, T_USAGE_B]}


def test_site_pack_refuses_two_templates_with_one_name(pack):
    """이름(name)이 같은 템플릿이 둘이면 사이트 팩을 읽을 때 오류다 — 하나를 말없이 덮어쓰지 않는다. 메시지는 이름과 두 폴더만."""
    shutil.copytree(pack / "templates" / T_LOADER, pack / "templates" / "zz_dup")
    _edit(pack, "zz_dup", lambda s: s.update(title="SECRET-TITLE"))
    with pytest.raises(TemplateError, match=f"'{T_LOADER}' 이 둘입니다") as e:
        SitePack(pack)
    assert "zz_dup/" in str(e.value) and "SECRET" not in str(e.value)


def test_concurrent_is_a_template_key(tmp_path):
    """Template.problems: concurrent 는 true/false, family 가 있을 때만 (template check 도 같은 목록)."""
    from minedocscan.tools.tpltools import check_template

    _img, spec = synth_usage.build_usage_log()
    d = tmp_path / "t"
    d.mkdir()
    imwrite(d / "reference.png", _img)
    for value, needle in ((True, "family 가 있을 때만"), ("true", "true/false"), (1, "true/false")):
        (d / "template.yaml").write_text(yaml.safe_dump(dict(spec, concurrent=value)), encoding="utf-8")
        with pytest.raises(TemplateError, match=needle):
            Template(d / "template.yaml")
        assert any(needle in e for e in check_template(d))
    (d / "template.yaml").write_text(yaml.safe_dump(dict(spec, concurrent=False)), encoding="utf-8")
    assert not Template(d / "template.yaml").concurrent and check_template(d) == []


# ── 분류: 묶음은 한 후보, 묶음이 없으면 예전과 같다 ───────────────────────────────────
def _old_classify(scores: dict[str, int], min_inliers: int = 60) -> tuple:
    """묶음을 모르던 때의 규칙 (forms/classify.py 의 예전 코드 그대로): 점수 순으로 1위, 1위/2위."""
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    second = ranked[1][1] if len(ranked) > 1 else 0
    return ranked[0][0] if ranked[0][1] >= min_inliers else None, ranked[0][1] / max(second, 1)


def test_groups_are_one_candidate_and_without_groups_nothing_changes(usage_run, synth, null_run):
    """동시 판은 한 후보 (점수는 최댓값, 여유는 계열 사이) — 판끼리의 비율(≈ 1)이 low_margin 을 켜지 않는다. 묶음이 없는 템플릿의
    분류 점수·여유·결과는 예전 규칙과 같다: 묶음과 상관없는 로우더 쪽, 판이 없는 합성 사이트(점검표·운반)의 쪽 (DB 의 여유까지)."""
    vsite, s = usage_run["pipe"].site, usage_run["settings"]
    clf = usage_run["pipe"].classifier
    groups = vsite.concurrent_groups(None)
    names = list(vsite.templates)
    by_tpl: dict[str, list] = {}
    for p in _pages(usage_run["pipe"].con).values():
        by_tpl.setdefault(p["template_name"], []).append(p)
    for name in (T_LOADER, T_USAGE, T_USAGE_B):
        for p in sorted(by_tpl[name], key=lambda p: p["page_id"])[:1]:
            gray = load_page(p["source_path"], p["page_no"], s.dpi)
            plain, grouped = clf.classify(gray, candidates=names), clf.classify(gray, candidates=names, groups=groups)
            assert plain.scores == grouped.scores and plain.group is None
            assert (plain.template, plain.margin) == _old_classify(plain.scores)
            if name == T_LOADER:                                   # 묶음 밖의 양식: 그대로
                assert (grouped.template, grouped.margin, grouped.group) == (plain.template, plain.margin, None)
                continue
            assert grouped.group == [T_USAGE, T_USAGE_B] and grouped.template == plain.template
            assert grouped.scores[grouped.template] == max(grouped.scores[n] for n in USAGE_LOGS)
            other = max(v for n, v in grouped.scores.items() if n not in USAGE_LOGS)
            assert grouped.margin == grouped.scores[grouped.template] / max(other, 1)
            assert grouped.margin >= s.classify_min_margin > plain.margin         # 판끼리는 모양으로 못 가린다
            assert p["classify_margin"] == pytest.approx(grouped.margin)           # 파이프라인이 쓴 여유
    assert usage_run["pipe"].summary["low_margin"] == []
    # 판이 없는 사이트: 묶음이 없다 → 예전 규칙 그대로 (파이프라인이 DB 에 쓴 양식·여유도)
    site = null_run.site
    assert all(site.concurrent_groups(d["date"]) == {} for d in synth.truth["days"])
    pdf = sorted(synth.scans.glob("*.pdf"))[0]
    rows = {r["page_no"]: r for r in null_run.con.execute(
        "SELECT p.page_no, p.template_name, p.classify_margin FROM doc_page p JOIN doc_document d "
        "ON p.document_id = d.document_id WHERE d.source_name = ?", (pdf.stem,))}
    for no in (1, 2, max(rows)):                                           # 점검표, 일보, 행렬
        r = null_run.classifier.classify(load_page(pdf, no, s.dpi), groups=site.concurrent_groups(None))
        assert r.group is None
        assert (r.template, r.margin) == _old_classify(r.scores)
        assert (r.template, r.margin) == (rows[no]["template_name"], pytest.approx(rows[no]["classify_margin"]))


# ── 판 A 만 있으면: 판 B 의 쪽이 실패하거나 칸이 어긋난 채 적재된다 (이 시험이 막는 실패가 합성에 있다) ──────────────
def _centre(bbox) -> np.ndarray:
    x0, y0, x1, y1 = bbox
    return np.array([(x0 + x1) / 2, (y0 + y1) / 2, 1.0])


def test_with_variant_a_only_b_pages_fail_or_load_with_shifted_cells(usage_synth, usage_run):
    """판 A 의 템플릿만 있는 사이트에서 판 B 의 쪽은 판 A 로 분류되고 판 A 에만 정합된다 (파이프라인이 판 하나로 하는 일 —
    align_to_template 한 번). 그 결과가 align_failed 이거나, loaded 라면 판 B 정답 칸의 중심(템플릿 좌표로 옮긴 것)이 판 A 의 같은 칸과
    4 px 이상 어긋난다 — 묶음 전체에서 하나 이상. 칸의 중심을 옮기는 법: 판 B 템플릿 좌표 → (두 판 실행에서 판 B 에 정합한 호모그래피의
    역) → 쪽 → (판 A 에 정합한 호모그래피) → 판 A 템플릿 좌표. 파이프라인을 다시 돌리지 않는다."""
    site = usage_run["pipe"].site
    a, b = site.templates[T_USAGE], site.templates[T_USAGE_B]
    clf = FormClassifier([t for n, t in site.templates.items() if n != T_USAGE_B])          # 판 B 가 없는 사이트의 후보
    pages = [p for p in _pages(usage_run["pipe"].con).values() if p["template_name"] == T_USAGE_B]
    truth = {t["source"]: t["template"] for t in usage_synth.truth["usage"]}
    assert len(pages) == 4 and all(truth[p["source"]] == T_USAGE_B for p in pages)
    cells_a = {(c.region, c.row, c.col): c for c in a.cells()}
    failed, shifted, stats = 0, 0, []
    for p in pages:
        gray = load_page(p["source_path"], p["page_no"], p["render_dpi"])
        assert clf.classify(gray).template == T_USAGE                                     # 판 A 로 분류된다
        ar = align_to_template(gray, a.reference, a.regions, ref_features=a.features)
        if not ar.ok:
            failed += 1
            stats.append(("align_failed", round(ar.grid_err_px, 1)))
            continue
        h_b = np.asarray(json.loads(p["homography"]))                                     # 쪽 → 판 B (두 판 실행)
        to_a = ar.homography @ np.linalg.inv(h_b)
        worst = 0.0
        for c in b.cells():
            if c.region == "fields" or (c.region, c.row, c.col) not in cells_a:
                continue
            q = to_a @ _centre(c.bbox)
            worst = max(worst, float(np.linalg.norm(q[:2] / q[2] - _centre(cells_a[(c.region, c.row, c.col)].bbox)[:2])))
        shifted += worst >= 4
        stats.append(("loaded", round(ar.grid_err_px, 1), round(worst, 1)))
    assert failed + shifted >= 1, stats
    assert failed + shifted == len(pages), stats             # 합성에서는 모든 쪽이 그렇다 (판 B 의 표는 10–15 px 아래)


# ── 두 판을 두면: 모든 쪽이 loaded, 고른 판 = 정답, 정합 횟수 ─────────────────────────────────
def test_with_both_variants_every_page_loads_as_its_own_variant(usage_synth, usage_run):
    """두 판(같은 family, concurrent)을 두면 모든 쪽이 loaded 이고 고른 판이 합성 정답과 같다. doc_page 에는 고른 판의 정합 수치와
    판마다의 괘선 오차(variant_errs)가, 업무 테이블의 source_form 에는 판의 이름이 남는다. 정답의 판이 날마다 섞였다."""
    con = usage_run["pipe"].con
    pages = _pages(con)
    truth = usage_synth.truth["usage"]
    assert len(pages) == len(truth) and {p["status"] for p in pages.values()} == {"loaded"}
    by_day: dict[str, set] = {}
    for t in truth:
        p = pages[t["source"]]
        assert p["template_name"] == t["template"], t["source"]
        if t["template"] == T_LOADER:
            assert p["variant_errs"] is None
            continue
        by_day.setdefault(t["date"], set()).add(t["template"])
        # 유한하지 않은 오차는 null 로 적는다 (Infinity 가 아니다). null 이 생기는 쪽은 OpenCV 판마다 달라 여기서 세지 않는다 —
        # 판이 모두 정합에 실패한 쪽의 null 은 test_after_alignment_everything_uses_the_chosen_variant
        errs = json.loads(p["variant_errs"], parse_constant=lambda c: pytest.fail(f"유한하지 않은 값 {c}"))
        assert set(errs) == set(USAGE_LOGS) and errs[p["template_name"]] == p["align_grid_err"]
        other = errs[next(n for n in USAGE_LOGS if n != p["template_name"])]
        assert other is None or other - errs[p["template_name"]] >= 4, errs            # 합성: 4 px 이상 차이
    assert all(v == set(USAGE_LOGS) for v in by_day.values()) and len(by_day) == 3        # 날마다 두 판이 섞였다
    forms = dict(con.execute("SELECT page_id, source_form FROM eq_usage_daily"))
    assert all(forms[p["page_id"]] == p["template_name"] for p in pages.values())


def test_single_variant_forms_align_once_and_variants_once_each(usage_run):
    """판이 하나뿐인 양식(로우더)은 쪽마다 정합을 한 번 한다 (지금과 같다). 묶인 운행일보는 쪽마다 판마다 한 번 (세션 실행에서 센 수)."""
    con = usage_run["pipe"].con
    n = Counter(r[0] for r in con.execute("SELECT template_name FROM doc_page"))
    usage = n[T_USAGE] + n[T_USAGE_B]
    assert n[T_LOADER] == 6 and n[T_USAGE] == 5 and n[T_USAGE_B] == 4
    assert usage_run["align_calls"] == Counter({T_LOADER: n[T_LOADER], T_USAGE: usage, T_USAGE_B: usage})


PAGE_TABLES = ("doc_page", "doc_page_meta", "doc_field", "eq_usage_daily", "prod_tally")


def _page_rows(con, pid: str) -> dict[str, list]:
    return {t: sorted(map(tuple, con.execute(f"SELECT * FROM {t} WHERE page_id = ?", (pid,))), key=repr) for t in PAGE_TABLES}


def test_after_alignment_everything_uses_the_chosen_variant(usage_synth, usage_run, tmp_path, monkeypatch):
    """분류가 묶음 안에서 틀린 판을 1위로 올려도(모양으로는 판을 못 가린다 — 1위/2위 ≈ 1) 정합 뒤의 모든 것은 고른 판으로 한다:
    template_name·print_sha(그 판의 인쇄 층)·칸·핸들러·source_form·검수 대기. 쪽 하나씩 새 DB 에 다시 적재해 세션 실행(분류가 맞은 판을
    1위로 올린 실행)의 행과 바이트까지 같은지 본다 — 판 A 쪽과 판 B 쪽 하나씩. 정답 인식기는 판 이름으로 찾으므로(사이트 팩 없이)
    틀린 판의 칸으로 읽으면 값이 빠진다. 통과한 판이 없으면 같은 규칙으로 고른 판의 수치로 align_failed, 판마다의 오차는 null."""
    from minedocscan.forms.classify import ClassResult
    from minedocscan.pipeline import Pipeline

    site = usage_run["pipe"].site
    pipe = Pipeline(replace(usage_run["settings"], work_root=tmp_path / "w", reviews=tmp_path / "r.jsonl"), site=site,
                    recognizer=usage_run["pipe"].recognizer)
    pages = _pages(usage_run["pipe"].con)
    sha = usage_synth.truth["print_layers"]
    for chosen, wrong in ((T_USAGE_B, T_USAGE), (T_USAGE, T_USAGE_B)):
        p = next(p for p in sorted(pages.values(), key=lambda p: p["page_id"]) if p["template_name"] == chosen)
        monkeypatch.setattr(pipe.classifier, "classify", lambda *a, _w=wrong, _m=p["classify_margin"], **k: ClassResult(
            _w, {}, _m, [T_USAGE, T_USAGE_B]))
        src = Path(p["source_path"])
        doc = pipe._document_row(p["document_id"], src, src.stem, "received", None)
        pipe.con.execute("INSERT OR REPLACE INTO doc_document (" + ", ".join(doc) + ") VALUES (" + ", ".join("?" * len(doc)) + ")",
                         tuple(doc.values()))
        out = pipe.process_page(p["document_id"], src.stem, p["page_no"], load_page(src, p["page_no"], p["render_dpi"]),
                                strict=True, source_path=src)
        assert out["status"] == "loaded"
        got, want = _page_rows(pipe.con, p["page_id"]), _page_rows(usage_run["pipe"].con, p["page_id"])
        assert all(want[t] for t in ("doc_page", "doc_field", "eq_usage_daily")), want
        assert got == want
        row = pipe.con.execute("SELECT template_name, print_sha FROM doc_page WHERE page_id = ?", (p["page_id"],)).fetchone()
        assert tuple(row) == (chosen, sha[chosen])
    # 통과한 판이 없는 묶음 (흰 쪽): 이름이 앞인 판의 수치로 align_failed, 판마다의 오차는 null
    monkeypatch.setattr(pipe.classifier, "classify", lambda *a, **k: ClassResult(T_USAGE_B, {}, 9.0, [T_USAGE, T_USAGE_B]))
    out = pipe.process_page("white", "white", 1, np.full((2339, 1654), 255, np.uint8), strict=True)
    row = dict(pipe.con.execute("SELECT * FROM doc_page WHERE page_id = 'white-p1'").fetchone())
    assert (out["status"], row["status"], row["template_name"], row["align_grid_err"], row["print_sha"]) == \
        ("align_failed", "align_failed", T_USAGE, None, None)
    assert json.loads(row["variant_errs"]) == {T_USAGE: None, T_USAGE_B: None}


def test_choose_variant_rule():
    """통과한 판 중 괘선 오차가 가장 작은 판. 0.5 px 안이면 인라이어가 많은 판, 같으면 이름 순서. 통과한 판이 없으면 같은 규칙으로."""
    def r(name, err, inl, ok=True):
        return (SimpleNamespace(name=name), SimpleNamespace(grid_err_px=err, n_inliers=inl, ok=ok))

    def pick(*rs):
        return choose_variant(list(rs))[0].name

    assert pick(r("a", 2.0, 900), r("b", 1.0, 500)) == "b"
    assert pick(r("a", 1.5, 900), r("b", 1.0, 500)) == "a"                 # 0.5 px 안 → 인라이어
    assert pick(r("a", 1.0, 700), r("b", 1.0, 700)) == "a"                 # 같으면 이름 순서
    assert pick(r("a", 0.0, 900, ok=False), r("b", 5.5, 100)) == "b"      # 통과한 판만
    assert pick(r("a", 9.0, 900, ok=False), r("b", float("inf"), 990, ok=False)) == "a"
    assert pick(r("a", float("inf"), 10, ok=False), r("b", float("inf"), 20, ok=False)) == "b"


# ── 정답은 계열로 찾는다 (eval, export-answers 로 만든 정답, oracle) ──────────────────────────
def test_answers_written_for_one_variant_find_the_other(usage_run):
    """정답을 템플릿 이름으로 찾는 곳은 동시 판을 계열로 묶는다 (site.answer_key): 판 A 의 이름으로 적힌 정답이 판 B 로 적재된 쪽에도
    붙는다. 사이트 팩이 없으면 예전처럼 이름 그대로."""
    con, site, answers = usage_run["pipe"].con, usage_run["pipe"].site, usage_run["answers"]
    as_a = {(o, T_USAGE if t == T_USAGE_B else t, *rest): v for (o, t, *rest), v in answers.items()}
    assert len(as_a) == len(answers) and any(k[1] == T_USAGE_B for k in answers)
    right = evaluate_fields(con, answers, site=site)
    renamed = evaluate_fields(con, as_a, site=site)
    assert right["answers_not_in_db"] == renamed["answers_not_in_db"] == 0
    assert renamed == right
    assert evaluate_fields(con, as_a)["answers_not_in_db"] > 0                     # 사이트 팩 없이: 판 B 의 쪽을 못 찾는다
    assert evaluate_fields(con, answers) == right                                   # 맞는 이름이면 사이트 팩과 상관없다
    src, _t, region, name, row_key = next(k for k in answers if k[1] == T_USAGE_B and k[2] == "meter")
    ctx = CellContext(T_USAGE_B, region, name, "handwritten_number", row_key, source=src)
    assert OracleRecognizer(as_a, site=site).recognize([None], [ctx])[0].text == answers[(src, T_USAGE_B, region, name, row_key)]
    assert OracleRecognizer(as_a).recognize([None], [ctx])[0].text == ""


# ── 리포트·pages --variants ───────────────────────────────────────────────────────
def test_report_counts_chosen_variants_and_near_ties(usage_run, null_run, capsys):
    """리포트: 판의 묶음마다 판마다 고른 쪽, 정합 실패, 두 판의 오차 차이가 1 px 미만인 쪽(두 오차가 모두 유한할 때만, 고른 쪽 중에서 —
    두 판 모두 정합에 실패한 쪽은 판을 고르지 않았으므로 정합 실패로만 센다). 판이 없는 사이트의 리포트에는 키가 없다 (regress 의 기준
    그대로). pages --variants 는 오차 차이가 1 px 미만인 쪽을 판마다의 오차·상태와 함께 (값 없이) — 정합 실패 쪽도 상태와 함께 나온다."""
    con = clone_db(usage_run["pipe"].con)
    families = usage_run["pipe"].site.variant_families()
    assert families == {T_USAGE: T_USAGE, T_USAGE_B: T_USAGE}
    rep = build_report(con, families)                                  # 계열마다 (사이트 팩이 있을 때 — run·report·regress)
    assert rep["variants"] == {T_USAGE: {"chosen": {T_USAGE: 5, T_USAGE_B: 4}, "align_failed": 0, "near_tie": 0}}
    assert f"동시 판 {T_USAGE}: 고른 쪽 {T_USAGE} 5, {T_USAGE_B} 4, 정합 실패 0" in format_report(rep)
    assert build_report(con)["variants"] == {GROUP: rep["variants"][T_USAGE]}      # 사이트 팩 없이: 정합한 판 이름들로
    assert "variants" not in build_report(null_run.con)
    ids = [r[0] for r in con.execute("SELECT page_id FROM doc_page WHERE variant_errs IS NOT NULL ORDER BY page_id")]
    con.execute("UPDATE doc_page SET variant_errs = ? WHERE page_id = ?", (json.dumps({T_USAGE: 0.0, T_USAGE_B: 0.5}), ids[0]))
    con.execute("UPDATE doc_page SET variant_errs = ? WHERE page_id = ?", (json.dumps({T_USAGE: 1.0, T_USAGE_B: 0.0}), ids[1]))
    con.execute("UPDATE doc_page SET variant_errs = ?, status = 'align_failed' WHERE page_id = ?",
                (json.dumps({T_USAGE: 7.0, T_USAGE_B: None}), ids[2]))
    con.execute("UPDATE doc_page SET variant_errs = ?, status = 'align_failed' WHERE page_id = ?",       # 둘 다 실패, 오차가 비슷
                (json.dumps({T_USAGE: 7.0, T_USAGE_B: 7.5}), ids[3]))
    con.commit()
    v = build_report(con, families)["variants"][T_USAGE]
    assert v["near_tie"] == 1 and v["align_failed"] == 2 and sum(v["chosen"].values()) == 7
    rows = {r["page_id"]: r for r in list_pages(con, variants=True)}
    assert sorted(rows) == sorted(ids[:1] + ids[3:4]) and rows[ids[0]]["variant_errs"] == {T_USAGE: 0.0, T_USAGE_B: 0.5}
    assert rows[ids[3]]["status"] == "align_failed"
    assert list_pages(con, variants=False)[0].get("variant_errs") is None
    db = usage_run["root"] / "variants.db"
    out = sqlite3.connect(db)
    con.backup(out)
    out.close()
    assert main(["pages", "--variants", "--db-url", f"sqlite:///{db}", "--json"]) == 0
    assert sorted(r["page_id"] for r in json.loads(capsys.readouterr().out)["pages"]) == sorted(rows)
    assert main(["pages", "--variants", "--db-url", f"sqlite:///{db}"]) == 0
    assert f"판마다 괘선 오차: {T_USAGE} 0.0, {T_USAGE_B} 0.5" in capsys.readouterr().out
    # report 명령은 사이트 팩으로 계열을 안다
    site_args = ["--site", str(usage_run["settings"].site), "--db-url", f"sqlite:///{db}"]
    assert main(["report", "--json", *site_args]) == 0
    assert json.loads(capsys.readouterr().out)["report"]["variants"] == {T_USAGE: v}
    assert main(["report", *site_args]) == 0
    c = v["chosen"]
    assert f"동시 판 {T_USAGE}: 고른 쪽 {T_USAGE} {c[T_USAGE]}, {T_USAGE_B} {c[T_USAGE_B]}, 정합 실패 2" in capsys.readouterr().out


# ── template variant ──────────────────────────────────────────────────────────────
CLEAN_SEEDS = (11, 1)         # 스캔 효과의 씨앗. 1 은 OpenCV 5.0 에서 쪽 전체의 특징점으로 구하면 괘선이 6–18 px 어긋나는 쪽이다


@pytest.fixture(scope="module")
def clean_b(tmp_path_factory) -> dict:
    """판 A 만 있는 합성 사이트 팩(판 B 없이, family·concurrent 없이)과 판 B 의 깨끗한 쪽들 (생성기의 빈 판 B 에 스캔 효과 —
    씨앗마다 한 장)."""
    root = tmp_path_factory.mktemp("variant_cmd")
    site = write_site_pack(root / "site", usage=True)
    img, spec = synth_usage.build_usage_log("b")
    scans = {}
    for seed in CLEAN_SEEDS:
        scans[seed] = root / "scans" / f"clean_b{seed}.png"
        imwrite(scans[seed], scan_effect(img, np.random.default_rng(seed)))
    return {"root": root, "site": site, "scan": scans[CLEAN_SEEDS[0]], "scans": scans, "spec": spec, "blank": img}


def _shift(a: np.ndarray, b: np.ndarray) -> float:
    """두 그림 조각의 평행 이동 (px, phaseCorrelate)."""
    (dx, dy), _r = cv2.phaseCorrelate(np.float32(a), np.float32(b))
    return float(np.hypot(dx, dy))


def test_variant_from_a_clean_b_page_matches_the_generator(clean_b, capsys):
    """판 B 의 깨끗한 합성 쪽으로 `template variant` 를 돌리면 표마다 다시 잡은 괘선(grid.ys·xs)이 생성기의 판 B 괘선과 2 px 안에서 같다
    (머리에 맞춰 편 그림이므로 생성기의 판 B 와 같은 좌표계). 쓴 기준 그림(reference.png)도 그 좌표계다: 표 밖 필드의 bbox 둘레가
    생성기의 판 B 와 0.5 px 안에서 겹치고, 그 그림에서 다시 잡은 괘선이 생성기의 괘선과 2 px 안. 열·행·필드는 그대로, family 는
    기존 판의 이름, 새 판에만 concurrent: true, print_image 는 복사하지 않는다. 기존 판의 파일은 바이트까지 그대로이고, 적을 두 줄을
    안내한다. 만든 판으로 사이트 팩이 읽히고 그날의 묶음이 된다. 씨앗 두 개 (CLEAN_SEEDS)."""
    from minedocscan.imaging.grid import detect_grid_roi

    site, tdir = clean_b["site"], clean_b["site"] / "templates" / T_USAGE
    shutil.copy(tdir / "reference.png", tdir / "print.png")                  # 기존 판에 인쇄 층이 있어도 복사하지 않는다
    _edit(site, T_USAGE, lambda s: s.update(print_image="print.png"))
    before = {p.name: p.read_bytes() for p in tdir.iterdir()}
    spec_b, blank = clean_b["spec"], clean_b["blank"]
    names = []
    for seed, scan in clean_b["scans"].items():
        name = f"usage_b{seed}"
        names.append(name)
        assert main(["template", "variant", str(tdir), "--scan", str(scan), "--name", name]) == 0
        out = capsys.readouterr().out
        assert "family: synth_usage_log" in out and "concurrent: true" in out and "사람이 고칠 것" not in out
        assert {p.name: p.read_bytes() for p in tdir.iterdir()} == before
        new = Template(site / "templates" / name / "template.yaml")
        ref = Template(new.dir / "template.yaml").reference                   # 디스크에 쓴 기준 그림
        for reg, want in zip(new.regions, spec_b["regions"], strict=True):
            ys, xs = want["grid"]["ys"], want["grid"]["xs"]
            found = dict(zip(("ys", "xs"), detect_grid_roi(ref, (xs[0] - 20, ys[0] - 20, xs[-1] + 21, ys[-1] + 21)),
                             strict=True))
            for axis in ("ys", "xs"):
                exp = np.array(want["grid"][axis])
                for got in (np.array(reg["grid"][axis]), np.array(found[axis])):
                    assert got.shape == exp.shape and np.abs(got - exp).max() <= 2, (seed, reg["name"], axis, got, exp)
            assert reg["columns"] == want["columns"] and reg["rows"] == want["rows"] and reg["role"] == want["role"]
        assert new.fields == spec_b["fields"]
        for f in spec_b["fields"]:                       # 필드의 줄(인쇄된 이름표·상자 — 쪽 너비 전체)이 같은 자리
            _x0, y0, _x1, y1 = f["bbox"]
            win = slice(y0 - 40, y1 + 40)
            assert _shift(ref[win], blank[win]) < 0.5, (seed, f["name"], _shift(ref[win], blank[win]))
        assert (new.family, new.concurrent, new.print_path) == (T_USAGE, True, None)
        assert sorted(p.name for p in new.dir.iterdir()) == ["reference.png", "template.yaml"]
        assert ref.shape == Template(tdir / "template.yaml").reference.shape
    # 사람이 기존 판에 두 줄을 적으면 판들이 한 묶음이다
    _edit(site, T_USAGE, lambda s: s.update(family=T_USAGE, concurrent=True))
    assert SitePack(site).concurrent_groups("2030-01-07") == {T_USAGE: sorted([T_USAGE, *names])}


def test_variant_homography_uses_only_features_outside_the_tables(clean_b, usage_synth, usage_run, monkeypatch):
    """새 기준 그림의 호모그래피는 표 영역(괘선 범위 + TABLE_MARGIN) 밖의 특징점만으로 구한다 (4.6): RANSAC 에 들어가는 기준 쪽의 점이
    하나도 표 영역에 없다. 표 영역 안의 점을 쓰면 RANSAC 이 표 쪽으로 타협한다 — 손글씨가 채워진 판 B 쪽에서 쪽 전체로 구하면 괘선이
    어긋나는 쪽이 있다 (OpenCV 5.0 에서 최대 150 px, 4.9 에서 3 px). 깨끗한 쪽과 채워진 판 B 쪽 하나로."""
    from minedocscan.tools import variant

    tpl = Template(clean_b["site"] / "templates" / T_USAGE / "template.yaml")
    h, w = tpl.reference.shape
    boxes = [variant._table_box(r, variant.TABLE_MARGIN, w, h) for r in tpl.regions]
    seen: list[np.ndarray] = []
    find = cv2.findHomography

    def spy(src, dst, *a, **kw):
        seen.append(np.asarray(dst).reshape(-1, 2))
        return find(src, dst, *a, **kw)

    monkeypatch.setattr(variant.cv2, "findHomography", spy)
    b_page = next(p for p in _pages(usage_run["pipe"].con).values() if p["template_name"] == T_USAGE_B)
    for gray in (cv2.imread(str(clean_b["scan"]), cv2.IMREAD_GRAYSCALE),
                 load_page(b_page["source_path"], b_page["page_no"], b_page["render_dpi"])):
        H, inliers = variant.header_homography(gray, tpl.reference, tpl.regions)
        assert H is not None and inliers >= variant.MIN_INLIERS
    assert len(seen) == 2
    for pts in seen:
        inside = [(x, y) for x, y in pts for x0, y0, x1, y1 in boxes if x0 <= x < x1 and y0 <= y < y1]
        assert len(pts) >= 12 and not inside, inside[:5]


def test_variant_pairs_lines_within_half_the_gap_and_reports_unpaired_lines():
    """괘선의 짝은 ±min(MAX_SHIFT, 이웃 표까지 간격의 절반) 안에서만 (4.6). 그린 그림: 표 둘이 30 px 떨어져 있고 위 표의 마지막 괘선이
    지워졌다 — 반경이 40 px 이면 그 괘선이 아래 표의 첫 괘선과 짝지어지지만, 15 px 이라 짝이 없어 기존 괘선을 그대로 두고 알린다.
    나란히 놓인 표(다른 축의 범위가 겹치지 않는 표)는 반경을 줄이지 않는다. 짝이 다 있는 아래 표는 옮겨 그린 자리로 다시 잡는다."""
    from minedocscan.tools.variant import MAX_SHIFT, _radius, _redetect

    top = {"name": "top", "grid": {"ys": [100, 160, 220], "xs": [100, 400, 700]}}
    low = {"name": "low", "grid": {"ys": [250, 300], "xs": [100, 400, 700]}}
    side = {"name": "side", "grid": {"ys": [100, 300], "xs": [900, 1100]}}
    assert _radius(top["grid"]["ys"], (100, 700), [(low["grid"]["ys"], (100, 700))]) == 15
    assert _radius(top["grid"]["ys"], (100, 700), [(side["grid"]["ys"], (900, 1100))]) == MAX_SHIFT
    assert _radius(top["grid"]["xs"], (100, 220), [(low["grid"]["xs"], (250, 300))]) == MAX_SHIFT     # 위아래 표의 세로 괘선
    img = np.full((400, 1200), 255, np.uint8)
    for y in (100, 160):                                         # 위 표: 마지막 가로 괘선(220)이 없다
        cv2.line(img, (100, y), (700, y), 0, 2)
    for x in (100, 400, 700):
        cv2.line(img, (x, 100), (x, 220), 0, 2)
    for y in (254, 304):                                         # 아래 표: 4 px 아래로 옮겨 그렸다
        cv2.line(img, (100, y), (700, y), 0, 2)
    for x in (100, 400, 700):
        cv2.line(img, (x, 254), (x, 304), 0, 2)
    t, lo = _copy(top), _copy(low)
    r = _redetect(img, t, [low, side])
    assert not r["redetected"] and "짝이 없는 괘선 1개" in r["reason"] and t["grid"] == top["grid"], r
    r = _redetect(img, lo, [top, side])
    assert r["redetected"] and all(abs(a - b) <= 1 for a, b in zip(lo["grid"]["ys"], [254, 304], strict=True)), (r, lo)
    assert lo["grid"]["xs"] == [100, 400, 700] or max(abs(a - b) for a, b in zip(lo["grid"]["xs"], [100, 400, 700],
                                                                                 strict=True)) <= 1


def _copy(reg: dict) -> dict:
    return json.loads(json.dumps(reg))


def test_variant_refuses_repo_existing_folder_and_reports_tables_it_cannot_redo(clean_b, tmp_path):
    """출력이 git 작업 트리 안이면(기준 그림은 현장 스캔) 거절, 이미 있는 폴더도 거절 — 한 줄, 아무것도 쓰지 않는다. 나눔 선이 있는 표
    (로우더 작업량)는 기존 괘선을 그대로 두고 사람이 고치도록 알린다. 짝이 없는 괘선도 같다 (_pair)."""
    site = clean_b["site"]
    repo_out = Path(__file__).parent / "variant-should-not-exist"
    for args, needle in (([*_args(clean_b), "--out-dir", str(repo_out)], "git 작업 트리"),
                         ([*_args(clean_b), "--out-dir", str(site / "templates" / T_LOADER)], "이미 있습니다"),
                         ([*_args(clean_b, name="synth_usage_log")], "같습니다"),
                         ([*_args(clean_b, scan=tmp_path / "none.png"), "--name", "x2"], "스캔을 읽을 수 없습니다")):
        with pytest.raises(SystemExit) as e:
            main(["template", "variant", *args])
        assert needle in str(e.value.code) and "\n" not in str(e.value.code), (needle, e.value.code)
    assert not repo_out.exists()
    blank = np.full((2339, 1654), 255, np.uint8)
    imwrite(tmp_path / "white.png", blank)
    with pytest.raises(VariantError, match="인라이어"):
        make_variant(site / "templates" / T_USAGE, tmp_path / "white.png", 1, "x3", out_dir=tmp_path / "x3")
    assert not (tmp_path / "x3").exists()
    img, _spec = synth_usage.build_loader_log()
    imwrite(tmp_path / "loader.png", scan_effect(img, np.random.default_rng(3)))
    r = make_variant(site / "templates" / T_LOADER, tmp_path / "loader.png", 1, "loader_b", out_dir=tmp_path / "loader_b")
    assert r["fix_by_hand"] == ["tally"] and "나눔 선" in next(t["reason"] for t in r["tables"] if t["region"] == "tally")
    old, new = Template(site / "templates" / T_LOADER / "template.yaml"), Template(tmp_path / "loader_b" / "template.yaml")
    assert new.region("tally")["grid"] == old.region("tally")["grid"]
    assert _pair([100, 150, 200], [101, 149], 20) == ([101, 149, 200], 1)          # 짝 없는 괘선
    assert _pair([100, 150], [125], 30)[1] == 1                                      # 두 괘선이 한 괘선과 → 틀린 짝


def test_variant_refuses_a_taken_name_and_unreadable_templates_in_one_line(clean_b, tmp_path):
    """--name 이 옆 템플릿(기존 판의 templates 폴더, 출력 폴더의 부모)의 이름이면 거절한다 — 이름이 둘이면 사이트 팩이 읽히지 않는다.
    폴더 이름이 달라도 template.yaml 의 name 으로 본다. 깨진 기존 판(YAML 문법, 이름 없음, 기준 이미지 없음·깨짐)은 traceback 이 아니라
    한 줄로 거절하고 template check 를 안내한다. 아무것도 쓰지 않는다."""
    site = clean_b["site"]
    with pytest.raises(SystemExit) as e:
        main(["template", "variant", *_args(clean_b, name=T_LOADER), "--out-dir", str(tmp_path / "elsewhere")])
    assert "이미 다른 템플릿이 씁니다" in str(e.value.code) and "\n" not in str(e.value.code), e.value.code
    assert not (tmp_path / "elsewhere").exists()
    renamed = tmp_path / "pack" / "templates"                              # 폴더 이름 ≠ name
    shutil.copytree(site / "templates" / T_USAGE, renamed / T_USAGE)
    shutil.copytree(site / "templates" / T_LOADER, renamed / "zz_other")
    with pytest.raises(VariantError, match="zz_other/"):
        make_variant(renamed / T_USAGE, clean_b["scan"], 1, T_LOADER)
    assert not (renamed / T_LOADER).exists()

    def broken(case: str, fn) -> str:
        d = tmp_path / case / T_USAGE
        shutil.copytree(site / "templates" / T_USAGE, d)
        fn(d)
        with pytest.raises(SystemExit) as e:
            main(["template", "variant", str(d), "--scan", str(clean_b["scan"]), "--name", "x_b"])
        msg = str(e.value.code)
        assert msg and "\n" not in msg and "Traceback" not in msg and "template check" in msg, (case, msg)
        assert sorted(p.name for p in d.parent.iterdir()) == [T_USAGE], case
        return msg

    assert "기준 이미지를 읽을 수 없습니다" in broken("no_ref", lambda d: (d / "reference.png").unlink())
    assert "기준 이미지를 읽을 수 없습니다" in broken("bad_ref", lambda d: (d / "reference.png").write_bytes(b"not a png"))
    assert "YAML 을 읽을 수 없습니다" in broken("yaml", lambda d: (d / "template.yaml").write_text("name: [x\n",
                                                                                                    encoding="utf-8"))
    assert "KeyError" in broken("no_name", lambda d: (d / "template.yaml").write_text(
        "\n".join(line for line in (d / "template.yaml").read_text(encoding="utf-8").splitlines()
                  if not line.startswith("name:")), encoding="utf-8"))


def _args(clean_b, name: str = "usage_b2", scan: Path | None = None) -> list[str]:
    return [str(clean_b["site"] / "templates" / T_USAGE), "--scan", str(scan or clean_b["scan"]), "--name", name]


# ── 합성: --usage-variants ─────────────────────────────────────────────────────────
def test_synth_variants_only_change_the_template_of_usage_pages(tmp_path):
    """판을 섞는 것은 운행일보 쪽의 판 이름뿐이다 (따로 쓰는 난수): 쪽의 계획(장비·값·시나리오)은 섞지 않은 것과 같고, 쪽이 둘 이상인
    날은 두 판이 섞인다. 판 A 의 템플릿은 usage_variants 없이 만든 것과 바이트까지 같다 (family·concurrent 만 더한다)."""
    days = [f"2030-01-{d:02d}" for d in range(7, 17)]
    plain = synth_usage.plan_days(days, np.random.default_rng([0, 5005]))
    mixed = synth_usage.plan_days(days, np.random.default_rng([0, 5005]))
    synth_usage.assign_variants(mixed, np.random.default_rng([0, 5005, 2]))
    for a, b in zip(plain, mixed, strict=True):
        assert [(p.equipment, p.meter, p.tally, p.scenarios) for p in a] == [(p.equipment, p.meter, p.tally, p.scenarios) for p in b]
        assert all(p.template == q.template or (p.template, q.template) == (T_USAGE, T_USAGE_B) for p, q in zip(a, b, strict=True))
        used = Counter(q.template for q in b if q.template in USAGE_LOGS)
        assert sum(used.values()) < 2 or set(used) == set(USAGE_LOGS)
    a, b = write_site_pack(tmp_path / "a", usage=True), write_site_pack(tmp_path / "b", usage=True, usage_variants=True)
    sa = yaml.safe_load((a / "templates" / T_USAGE / "template.yaml").read_text(encoding="utf-8"))
    sb = yaml.safe_load((b / "templates" / T_USAGE / "template.yaml").read_text(encoding="utf-8"))
    assert sb.pop("family") == T_USAGE and sb.pop("concurrent") is True and sa == sb
    assert not (a / "templates" / T_USAGE_B).exists() and (b / "templates" / T_USAGE_B / "reference.png").is_file()
    assert (a / "templates" / T_USAGE / "reference.png").read_bytes() == (b / "templates" / T_USAGE / "reference.png").read_bytes()
    with pytest.raises(SystemExit, match="--usage-logs"):
        main(["synth", str(tmp_path / "x"), "--usage-variants"])
    with pytest.raises(ValueError, match="usage_logs"):
        generate(tmp_path / "y", days=1, usage_variants=True)
    assert {T_INSP, T_LOG} <= set(SitePack(b).templates)
