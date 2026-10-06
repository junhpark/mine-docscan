"""인쇄 층을 뺀 그림으로 값 유무를 (tasks/0006 단계 3) — 인쇄 층을 켠 것과 끈 것을 같은 쪽으로 비교한다.

켠 것 = usage_run (합성 가동 일보, 인쇄 층을 켠 사이트 팩으로 파이프라인을 돌린 것). 끈 것·틀린 층 = 같은 쪽을 저장된 호모그래피로
다시 편 그림(usage_pages — 파이프라인의 정합 그림과 바이트까지 같다)에서 칸을 다시 재고 핸들러만 다시 돌린 것 (reload_usage) —
파이프라인을 한 번 더 돌리지 않는다 (6절, 시간 예산). 그 경로가 파이프라인과 같다는 것을 먼저 보인다 (켠 층으로 다시 적재 = usage_run).
"""
from __future__ import annotations

import json
import shutil

import cv2
import numpy as np
import pytest
import yaml

from conftest import clone_db, reload_usage
from minedocscan.handlers.usage import UsageHandler, presence
from minedocscan.imaging import printlayer
from minedocscan.imaging.cells import observe_cells
from minedocscan.report import build_report, format_report
from minedocscan.review.export import export_crops
from minedocscan.review.server import ReviewApp
from minedocscan.review.store import Review, append, import_into
from minedocscan.tools.synth_usage import PRINTED_ITEMS, T_LOADER, T_USAGE, T_USAGE_B, USAGE_LOGS
from test_review_store import _dump

ROLE_REGIONS = ("meter", "shifts", "tally")
USAGE_DB_TABLES = ("doc_field", "eq_usage_daily", "prod_tally", "xcheck_usage")


@pytest.fixture(scope="module")
def onoff(usage_run, usage_pages) -> dict:
    """{"on": usage_run 의 DB, "off": 같은 쪽을 인쇄 층 없이 다시 적재한 DB (복사본), "relo": 켠 층으로 다시 적재한 DB (복사본)}."""
    on = usage_run["pipe"].con
    off = clone_db(on)
    reload_usage(off, usage_run, usage_pages, lambda pg: None)
    off.execute("UPDATE doc_page SET print_sha = NULL")
    relo = clone_db(on)
    reload_usage(relo, usage_run, usage_pages, lambda pg: pg["tpl"].print_mask)
    off.commit()                                    # 열린 쓰기 트랜잭션이 있으면 clone_db(backup)가 끝나지 않는다
    relo.commit()
    return {"on": on, "off": off, "relo": relo}


def _cells(con, answers) -> dict:
    """role 표(meter·shifts·tally)의 손으로 쓰는 형식 있는 칸: (출처, 표, 열, 행 키) → doc_field 행 + 정답(없으면 None)."""
    out = {}
    for r in con.execute("SELECT f.*, d.source_name || '#' || p.page_no AS source, p.template_name FROM doc_field f "
                         "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
                         f"WHERE f.region IN ({', '.join('?' * len(ROLE_REGIONS))}) AND f.kind LIKE 'handwritten%' "
                         "AND f.format IS NOT NULL", ROLE_REGIONS):
        key = (r["source"], r["region"], r["field_name"], r["row_key"])
        out[key] = dict(r) | {"truth": answers.get((r["source"], r["template_name"], r["region"], r["field_name"],
                                                    r["row_key"] or ""))}
    return out


def _printed_only(key) -> bool:
    """칸 안에 인쇄만 있는 칸 (값이 적히지 않은 것은 따로 본다): 작업량의 인쇄 칸(PRINTED_ITEMS 의 행), 근무 시각 칸("~")."""
    _src, region, _name, row_key = key
    return (region == "tally" and row_key.split("|")[0] in PRINTED_ITEMS) or region == "shifts"


def _auto_empty(f: dict) -> bool:
    return not f["has_value_raw"] and f["status_raw"] == "auto"


# ── 다시 적재하는 경로가 파이프라인과 같다 ─────────────────────────────────────
def test_reloading_the_same_pages_with_the_layer_reproduces_the_pipeline(onoff, usage_synth, usage_pages):
    """켠 층으로 다시 적재한 DB = 파이프라인의 DB (업무 테이블·검산까지). 그래서 끈 DB 도 파이프라인이 껐을 때와 같다.
    쪽마다 쓴 인쇄 층의 해시가 남고(doc_page.print_sha = 합성이 넣은 층 — 운행일보는 고른 판의 층), 리포트가 양식별로 센다."""
    on, relo = onoff["on"], onoff["relo"]
    for t in USAGE_DB_TABLES:
        assert _dump(relo, t) == _dump(on, t), t
    sha = usage_synth.truth["print_layers"]
    assert set(sha) == {T_USAGE, T_USAGE_B, T_LOADER} and len(set(sha.values())) == 3       # 판마다 따로 (4.1)
    pages = on.execute("SELECT page_id, template_name, print_sha, variant_errs FROM doc_page ORDER BY page_id").fetchall()
    assert len(pages) == len(usage_pages) == len(usage_synth.truth["usage"])
    assert all(p["print_sha"] == sha[p["template_name"]] for p in pages)
    assert all((p["variant_errs"] is None) == (p["template_name"] == T_LOADER) for p in pages)
    rep = build_report(on)
    assert rep["print_layer"] == {name: sum(p["template_name"] == name for p in pages)
                                  for name in (T_LOADER, T_USAGE, T_USAGE_B)}
    assert min(rep["print_layer"].values()) >= 4
    assert "인쇄 층으로 값 유무를 잰 쪽: " in format_report(rep)
    assert "print_layer" not in build_report(onoff["off"])                # 인쇄 층을 쓴 쪽이 없으면 키가 없다
    # 핸들러에서 오류가 난 쪽은 print_sha 가 남아도(어디까지 갔는지) 세지 않는다 — 그 쪽의 잰 값은 되돌려졌다
    err = clone_db(on)
    err.execute("UPDATE doc_page SET status = 'error' WHERE page_id = ?", (pages[0]["page_id"],))
    assert build_report(err)["print_layer"] == {
        name: n - (name == pages[0]["template_name"]) for name, n in rep["print_layer"].items()}


# ── 인쇄만 있는 칸: 끄면 검수 대기, 켜면 빈 칸으로 자동 적재 ──────────────────────────
def test_printed_only_cells_wait_for_review_off_and_are_auto_empty_on(onoff, usage_run, usage_synth):
    answers = usage_run["answers"]
    on, off = _cells(onoff["on"], answers), _cells(onoff["off"], answers)
    assert set(on) == set(off)
    printed = [k for k in on if _printed_only(k) and on[k]["truth"] is None]
    assert {k[1] for k in printed} == {"tally", "shifts"} and len(printed) >= 20
    assert all(off[k]["has_value_raw"] == 1 and off[k]["status_raw"] == "pending" for k in printed)
    assert all(on[k]["has_value_raw"] == 0 and on[k]["status_raw"] == "auto" and on[k]["value_final"] is None
               for k in printed)
    # 같은 칸, 같은 크롭 자리 — 다른 것은 잉크를 잰 그림뿐이다
    assert all(on[k]["x0"] == off[k]["x0"] and on[k]["ink"] <= off[k]["ink"] for k in printed)
    # 아무것도 쓰지 않은 SHOVEL 쪽: 끄면 근무 시각 칸("~")이 검수 대기라 그 쪽이 pending, 켜면 빈 쪽으로 자동 (0005 의 기대가 바뀐다)
    src = {t["source"] for t in usage_synth.truth["usage"] if "SHOVEL_nothing_written" in t["scenarios"]}
    assert src

    def usage(con):
        return {r["source"]: dict(r) for r in con.execute(
            "SELECT u.*, d.source_name || '#' || p.page_no AS source FROM eq_usage_daily u JOIN doc_page p "
            "ON u.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id")}

    u_on, u_off = usage(onoff["on"]), usage(onoff["off"])
    assert all(u_off[s]["review_status"] == "pending" and u_on[s]["review_status"] == "auto" for s in src)
    assert all(u_on[s]["shifts"] == "{}" and u_on[s]["hours"] is None for s in src)


# ── 값이 적힌 칸은 켜든 끄든 빈 칸으로 자동 적재되지 않는다 ─────────────────────────────
def test_written_cells_are_never_auto_emptied_on_or_off(onoff, usage_run):
    answers = usage_run["answers"]
    for name in ("on", "off"):
        cells = _cells(onoff[name], answers)
        written = {k: f for k, f in cells.items() if f["truth"] is not None}
        assert {k[1] for k in written} == set(ROLE_REGIONS), name
        two = [k for k, f in written.items() if k[1] == "tally" and _printed_only(k) and len(f["truth"]) == 2]
        assert len(two) >= 6, name                                          # 인쇄 사이의 두 자리 숫자
        assert not [k for k, f in written.items() if _auto_empty(f)], name
        # 정수 칸은 인식기(oracle)가 읽는다, 소수·시각 칸은 잉크 있음 + 검수 대기
        assert all(f["value_raw"] == f["truth"] and f["status_raw"] == "auto" for k, f in written.items() if k[1] == "tally")
        assert all(f["has_value_raw"] == 1 and f["status_raw"] == "pending" for k, f in written.items() if k[1] != "tally")


# ── 필드·작업 표는 인쇄 층을 쓰지 않는다 ─────────────────────────────────────────────
def test_fields_and_activities_are_measured_on_the_original_image(onoff, usage_pages):
    """role 표의 형식 있는 칸만 인쇄를 뺀 그림으로 잰다: 그 밖의 칸(필드·작업 표·인쇄된 칸)은 켜든 끄든 행이 바이트까지 같다 —
    모든 로우더 쪽의 같은 자리에 "ok" 를 쓴 점검란(인쇄 층에 잔상으로 들어간다)도. 크롭은 원래 그림 그대로다."""
    def rows(con, role_cells: bool):
        q = ("SELECT f.* FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id WHERE "
             + ("" if role_cells else "NOT ") + f"(f.region IN ({', '.join('?' * len(ROLE_REGIONS))}) "
             "AND f.kind LIKE 'handwritten%' AND f.format IS NOT NULL) ORDER BY f.field_id")
        return [tuple(r) for r in con.execute(q, ROLE_REGIONS)]

    on, off = rows(onoff["on"], False), rows(onoff["off"], False)
    assert on == off and len(on) > 300
    check = onoff["on"].execute("SELECT f.* FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                                "WHERE p.template_name = ? AND f.region = 'fields' AND f.field_name = 'check'",
                                (T_LOADER,)).fetchall()
    # 잉크 비율이 text_ink_min 경계 아래인 "ok" 는 끈 상태에서도 빈 칸이다 (글자 필드의 기존 한계 — 인쇄 층과 무관, 단계 3)
    assert len(check) == len([pg for pg in usage_pages if pg["tpl"].name == T_LOADER])
    assert all((r["value_raw"] == "ok") == (r["ink"] >= UsageHandler.text_ink_min) for r in check)
    assert sum(r["value_raw"] == "ok" for r in check) >= len(check) - 2
    # 점검란은 인쇄 층에 잔상이 남는다 — 그것을 썼다면 빈 칸이 되었을 칸이다 (필드에 쓰지 않는 이유, 4.3)
    tpl = next(pg["tpl"] for pg in usage_pages if pg["tpl"].name == T_LOADER)
    box = next(c.bbox for c in tpl.field_cells() if c.name == "check")
    assert printlayer.coverage(tpl.print_mask, [box])[0] > 0
    assert rows(onoff["on"], True) != rows(onoff["off"], True)               # role 칸은 달라진다 (인쇄만 있는 칸)
    # 크롭(CellObs.crop)은 마스크와 무관하게 원래 그림의 같은 화소
    pg = next(p for p in usage_pages if p["tpl"].name == T_LOADER)
    a, b = observe_cells(pg["aligned"], pg["tpl"], None), observe_cells(pg["aligned"], pg["tpl"], pg["tpl"].print_mask)
    assert all(np.array_equal(x.crop, y.crop) for x, y in zip(a, b, strict=True))
    assert [x.ink for x in a if not pg["tpl"].role_value_cell(x.cell)] == \
        [y.ink for y in b if not pg["tpl"].role_value_cell(y.cell)]


# ── 일부러 틀린 인쇄 층: 검수 대기가 늘 뿐, 값이 적힌 칸을 잃지 않는다 ──────────────────────
def _shift(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    out = np.zeros_like(mask)
    h, w = mask.shape
    out[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)] = mask[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)]
    return out


class _RoleCells:
    """템플릿을 role 표의 형식 있는 칸만 보이게 감싼다 (시험의 시간 — 층마다·쪽마다 잴 칸만). 그 칸의 잉크·덩어리 배정은 다른 칸과
    무관하다 (observe_cells 는 칸마다 따로 재고, presence 의 덩어리 배정은 표마다 그 표의 role 칸만 쓴다)."""

    def __init__(self, tpl):
        self._tpl = tpl
        self._cells = [c for c in tpl.cells() if tpl.role_value_cell(c)]

    def cells(self):
        return self._cells

    def field_cells(self):
        return []

    def __getattr__(self, name):
        return getattr(self._tpl, name)


_ROLE_ONLY: dict[str, _RoleCells] = {}


def _lost_and_back(pg, m, answers) -> tuple[list, list, int, int]:
    """한 쪽을 마스크 m 으로 잰 값 유무 (핸들러와 같은 함수 — handlers/usage.presence): (값이 적혔는데 "없음"인 role 칸,
    값이 적힌 칸의 표 이름들, 인쇄만 있는 칸 중 "있음"인 수, 인쇄만 있는 칸 수)."""
    tpl = _ROLE_ONLY.setdefault(pg["tpl"].name, _RoleCells(pg["tpl"]))
    obs = observe_cells(pg["aligned"], tpl, m)
    pres, _ = presence(pg["aligned"], tpl, obs, m)
    lost, written, back, n_printed = [], [], 0, 0
    for o, p in zip(obs, pres, strict=True):
        c = o.cell
        if not tpl.role_value_cell(c):
            continue
        if answers.get((pg["source"], tpl.name, c.region, c.name, c.row_key)) is not None:
            written.append(c.region)
            if not p.has_ink:
                lost.append((pg["source"], c.region, c.name))
        elif _printed_only((pg["source"], c.region, c.name, c.row_key)):
            n_printed += 1
            back += int(p.has_ink)
    return lost, written, back, n_printed


def test_wrong_layers_only_add_reviews(usage_pages, usage_run):
    """다른 양식의 인쇄 층(이 양식의 크기로 늘인 것)과 5 px 밀린 맞는 층 (+x, −x, +y, −y): role 표의 값이 적힌 칸은 어느 층에서도
    "있음"이다 (빈 칸으로 자동 적재되지 않는다). 인쇄만 있는 칸 일부는 "있음"으로 돌아간다 — 틀린 층의 결과는 검수 한 번 더다.
    (시험한 층에 대해서다 — 4.3. 구조로 보장되는 것은 아니다.) 값 유무는 핸들러와 같은 함수(handlers/usage.presence)로 잰다.
    맞는 층은 DB 로 본다 (test_written_cells…, test_printed_only… — 인쇄만 있는 칸은 전부 빈 칸).
    다른 양식의 층은 모든 쪽에, 밀린 층은 로우더 쪽에만 (시간 — 6절): 로우더 쪽에 세 표(계기·근무 시각·작업량)의 값이 적힌 칸과
    인쇄만 있는 칸이 다 있다. 운행일보의 계기 칸은 다른 양식의 층과 적은 쪽의 층(아래 시험)으로 본다."""
    answers = usage_run["answers"]
    layers = {pg["tpl"].name: pg["tpl"].print_layer for pg in usage_pages}

    cache: dict[str, dict] = {}

    def wrong(pg) -> dict:
        """그 양식의 틀린 층들 (양식마다 한 번 만든다)."""
        tpl = pg["tpl"]
        if tpl.name not in cache:
            h, w = tpl.reference.shape
            other = layers[T_USAGE if tpl.name == T_LOADER else T_LOADER]
            out = {"other": printlayer.mask(cv2.resize(other, (w, h), interpolation=cv2.INTER_AREA))}
            out |= {f"shift{dx:+d},{dy:+d}": _shift(tpl.print_mask, dx, dy)
                    for dx, dy in ((5, 0), (-5, 0), (0, 5), (0, -5))}
            cache[tpl.name] = out
        return cache[tpl.name]

    # role 칸만 보이게 감싼 템플릿(_RoleCells)으로 잰 값 유무 = 템플릿 전체로 잰 것의 role 칸 (한 쪽, 틀린 층으로)
    pg = next(p for p in usage_pages if p["tpl"].name == T_LOADER)
    m = wrong(pg)["other"]

    def judged(tpl):
        obs = observe_cells(pg["aligned"], tpl, m)
        return [(o.cell, o.ink, p) for o, p in zip(obs, presence(pg["aligned"], tpl, obs, m)[0], strict=True)
                if pg["tpl"].role_value_cell(o.cell)]

    part = judged(_RoleCells(pg["tpl"]))
    assert judged(pg["tpl"]) == part and len(part) > 20

    back: dict[str, int] = {}
    written: dict[str, list] = {}
    n_printed = 0
    for pg in usage_pages:
        for name, m in wrong(pg).items():
            if name != "other" and pg["tpl"].name != T_LOADER:
                continue
            lost, w, b, n = _lost_and_back(pg, m, answers)
            assert not lost, (name, lost)
            back[name] = back.get(name, 0) + b
            written.setdefault(name, []).extend(w)
            n_printed += n if name == "other" else 0
    assert len(written["other"]) >= 50 and n_printed >= 20
    assert all(set(w) == set(ROLE_REGIONS) for w in written.values())   # 밀린 층마다 세 표의 값이 적힌 칸을 잰다
    assert back["other"] > 0 and sum(back.values()) > back["other"]     # 틀린 층: 일부가 검수 대기로 돌아간다


# ── 적은 쪽으로 만든 인쇄 층 (4.3 의 "2–5장으로 만든 층") ───────────────────────────────────
FEW_PAGES_LOSE = ("2–3장으로 만든 층은 4.3 이 말한 것과 달리 값이 적힌 칸을 잃는다 (합성 3일치, 명령의 차례: 로우더 작업량의 두 자리 값 "
                  "2장 1칸, 3장 4칸 — 여러 쪽의 같은 자리에 쓴 손글씨가 75 백분위에 남는다). 리드·사람의 결정을 기다린다 "
                  "(MIN_PAGES 를 올리거나 4.3 을 '4장부터'로 고친다) — 결정되면 이 표시를 바꾼다")


@pytest.mark.parametrize("k", [pytest.param(2, marks=pytest.mark.xfail(strict=True, reason=FEW_PAGES_LOSE)),
                               pytest.param(3, marks=pytest.mark.xfail(strict=True, reason=FEW_PAGES_LOSE)), 4, 5])
def test_layers_from_few_pages_do_not_lose_written_values(k, usage_pages, usage_run):
    """template print-layer 가 경고와 함께 만드는 층 (5장 미만 — 4.2): 명령과 같은 차례(pick_order — 날짜별로 돌아가며)의 앞 k장을
    같은 방법(imaging/printlayer.estimate, 백분위 75 → mask)으로 겹친 층으로 그 양식의 모든 쪽을 잰다. 값이 적힌 role 칸을 잃지
    않아야 한다 (4.3, 7절). 다시 편 그림(usage_pages)은 명령이 다시 펴는 그림과 바이트까지 같다 (tools/printlayer.page_image)."""
    from minedocscan.tools.printlayer import candidate_pages, pick_order

    answers, con = usage_run["answers"], usage_run["pipe"].con
    n_written = 0
    for name in (T_LOADER, *USAGE_LOGS):                 # 로우더 먼저 — 잃는 칸은 작업량 칸이다. 운행일보는 판마다 (4–5쪽)
        pgs = {pg["page_id"]: pg for pg in usage_pages if pg["tpl"].name == name}
        order = [pgs[r["page_id"]] for r in pick_order(candidate_pages(con, name))]
        assert len(order) == len(pgs) >= 4
        m = printlayer.mask(printlayer.estimate([pg["aligned"] for pg in order[:k]]))
        lost = []
        for pg in order:
            ls, w, _b, _n = _lost_and_back(pg, m, answers)
            lost += ls
            n_written += len(w)
        assert not lost, (name, k, lost)
    assert n_written >= 50


# ── 크롭은 켜든 끄든 바이트까지 같다 ──────────────────────────────────────────────────
def test_crops_do_not_depend_on_the_layer(usage_run, usage_synth, onoff, tmp_path):
    """export-crops 와 검수 화면의 /crop: 같은 DB 를 인쇄 층이 있는 사이트 팩과 없는 사이트 팩으로, 그리고 인쇄 층을 끄고 잰 DB 를
    → PNG 가 바이트까지 같다. labels.jsonl 의 inked 는 인쇄만 있는 칸에서만 다르다: 끄면 True, 켜면 False (인식기에 가지 않는다 —
    의도한 것). 로우더 쪽 하나와 운행일보의 판마다 쪽 하나만 (원본을 다시 렌더링하는 시간을 아낀다)."""
    con = clone_db(usage_run["pipe"].con)
    plain = tmp_path / "plain"
    shutil.copytree(usage_synth.site, plain, ignore=shutil.ignore_patterns("print.png"))
    for name in (T_LOADER, *USAGE_LOGS):
        p = plain / "templates" / name / "template.yaml"
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        spec.pop("print_image")
        p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    from minedocscan.forms.sitepack import SitePack

    sites = {"on": usage_run["pipe"].site, "off": SitePack(plain)}
    assert sites["on"].templates[T_LOADER].uses_print_layer and not sites["off"].templates[T_LOADER].uses_print_layer
    cells = _cells(con, usage_run["answers"])
    pages = [sorted({f["page_id"] for f in cells.values() if f["template_name"] == name})[0]
             for name in (T_LOADER, *USAGE_LOGS)]
    pick = {k: f for k, f in cells.items() if f["page_id"] in pages}
    printed = [k for k, f in pick.items() if _printed_only(k) and f["truth"] is None]
    assert printed and any(f["truth"] for f in pick.values())
    path = tmp_path / "reviews.jsonl"
    for f in pick.values():
        append(path, Review(f["field_id"], "value", f["truth"], "jp") if f["truth"] else Review(f["field_id"], "empty",
                                                                                                reviewer="jp"))
    import_into(con, path)
    off_db = clone_db(onoff["off"])                                  # 인쇄 층을 끄고 잰 같은 쪽 (같은 field_id, 같은 검수)
    import_into(off_db, path)
    settings = usage_run["settings"]
    out = {}
    for name, (db, site) in {"on": (con, sites["on"]), "off": (con, sites["off"]), "off_db": (off_db, sites["off"])}.items():
        r = export_crops(db, site, settings, tmp_path / name)
        assert r["written"] == len(pick), r
        out[name] = {p.relative_to(tmp_path / name).as_posix(): p.read_bytes() for p in (tmp_path / name).rglob("*.png")}
    assert len(out["on"]) == len(pick) and out["on"] == out["off"] == out["off_db"]
    labels = {n: sorted((tmp_path / n).rglob("labels.jsonl")) for n in out}
    assert [p.read_bytes() for p in labels["on"]] == [p.read_bytes() for p in labels["off"]]
    rows = {n: {x["field_id"]: x for p in labels[n] for x in map(json.loads, p.read_text().splitlines())} for n in labels}
    inked = {n: {f: x["inked"] for f, x in rows[n].items()} for n in rows}
    printed_ids = {pick[k]["field_id"] for k in printed}
    assert all(not inked["on"][f] and inked["off_db"][f] for f in printed_ids)
    assert all(inked["on"][f["field_id"]] for f in pick.values() if f["truth"])
    # 그 밖은 줄까지 같다 (inked 를 빼고 — 인쇄만 있는 칸의 inked 만 다르다)
    assert {f: x | {"inked": None} for f, x in rows["on"].items()} == {f: x | {"inked": None} for f, x in rows["off_db"].items()}
    assert {f for f in rows["on"] if inked["on"][f] != inked["off_db"][f]} == printed_ids
    # 검수 화면의 /crop (칸, 행 띠)
    apps = {n: ReviewApp(con, site, settings, "jp", "pending") for n, site in sites.items()}
    for k in printed[:2]:
        for kind in ("cell", "row"):
            a, b = (apps[n].crop_png({"field_id": pick[k]["field_id"], "kind": kind}) for n in ("on", "off"))
            assert a == b and a[0][:8] == b"\x89PNG\r\n\x1a\n"


# ── 인쇄 층이 없는 템플릿: 예전과 같다 ──────────────────────────────────────────────
def test_without_a_mask_cells_and_blobs_are_unchanged(null_run):
    """print_mask=None 이면 observe_cells·assign_blobs 의 결과가 예전과 같다: 운반 일보 한 쪽을 null_run 의 DB(인쇄 층 없는 사이트)와
    비교하고 (잉크가 바이트까지 같다), 마스크를 주지 않은 호출과 빈 마스크(전부 False)를 준 호출이 같다."""
    from minedocscan.imaging.blobs import assign_blobs
    from minedocscan.tools.printlayer import page_image
    from minedocscan.tools.synth import T_LOG

    con, site, settings = null_run.con, null_run.site, null_run.settings
    assert not any(t.uses_print_layer for t in site.templates.values())
    assert con.execute("SELECT COUNT(*) FROM doc_page WHERE print_sha IS NOT NULL").fetchone()[0] == 0
    assert "print_layer" not in build_report(con)
    r = con.execute("SELECT p.*, d.source_path, d.source_rel FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
                    "WHERE p.template_name = ? AND p.status = 'loaded' ORDER BY p.page_id", (T_LOG,)).fetchone()
    tpl = site.templates[T_LOG]
    img = page_image(r, tpl, settings, tpl.reference.shape)[0]
    obs = observe_cells(img, tpl)
    db = {(f["region"], f["field_name"], f["row_no"]): f["ink"] for f in con.execute(
        "SELECT region, field_name, row_no, ink FROM doc_field WHERE page_id = ?", (r["page_id"],))}
    assert {(o.cell.region, o.cell.name, o.cell.row): o.ink for o in obs} == db
    empty = np.zeros(img.shape, bool)
    assert [o.ink for o in observe_cells(img, tpl, None)] == [o.ink for o in obs]
    reg = tpl.region("haul")
    cells = [c for c in tpl.cells() if c.region == "haul" and c.kind == "handwritten_number"]
    a = assign_blobs(img, cells, reg["grid"]["ys"], reg["grid"]["xs"])
    b = assign_blobs(img, cells, reg["grid"]["ys"], reg["grid"]["xs"], print_mask=empty)
    assert a[0] == b[0] and a[0] and [x.bbox for x in a[1]] == [x.bbox for x in b[1]]
    # 역할이 없는 표뿐인 양식은 마스크를 받아도 칸의 잉크를 그대로 잰다 (마스크는 role 표의 형식 있는 칸만)
    assert [o.ink for o in observe_cells(img, tpl, ~empty)] == [o.ink for o in obs]


# ── 합성: --print-layers ───────────────────────────────────────────────────────────
def test_synth_print_layers_only_add_the_layers(tmp_path, capsys):
    """synth --usage-only --print-layers: 두 가동 일보 템플릿에 print.png 와 print_image, truth 에 해시. 스캔 문서·정답·그 밖의
    파일은 인쇄 층 없이 만든 것과 바이트까지 같다 (난수를 더 쓰지 않는다 — 층은 그 스캔에서 결정적으로 나온다: 같은 씨앗의 스캔은
    바이트까지 같고(test_synth), 추정은 같은 쪽이면 같은 그림이다(test_printlayer)). 층은 생성기의 빈 그림이 아니라 스캔한 쪽에서
    추정한 것이다 (그래도 빈 양식의 인쇄 — 근무 시각 칸의 "~" 까지 — 를 덮는다). 1일치 (시간)."""
    from minedocscan.cli import main
    from minedocscan.forms.template import Template
    from minedocscan.imaging import grid
    from minedocscan.imaging.io import imread_gray
    from minedocscan.tools import synth_usage
    from minedocscan.tools.synth import generate

    with pytest.raises(SystemExit, match="--usage-logs"):
        main(["synth", str(tmp_path / "x"), "--print-layers"])
    with pytest.raises(ValueError, match="usage_logs"):
        generate(tmp_path / "y", days=1, print_layers=True)
    assert main(["synth", str(tmp_path / "b"), "--days", "1", "--usage-only", "--print-layers", "--json"]) == 0
    capsys.readouterr()
    generate(tmp_path / "c", days=1, usage_only=True)
    files = {d: {p.relative_to(tmp_path / d).as_posix(): p.read_bytes() for p in (tmp_path / d).rglob("*") if p.is_file()}
             for d in ("b", "c")}
    site = tmp_path / "b" / "site"
    sha = json.loads(files["b"]["truth.json"])["print_layers"]
    layered = {f"site/templates/{n}/{x}" for n in (T_USAGE, T_LOADER) for x in ("print.png", "template.yaml")}
    assert set(files["b"]) - set(files["c"]) == {f"site/templates/{n}/print.png" for n in (T_USAGE, T_LOADER)}
    assert {k: v for k, v in files["b"].items() if k not in layered | {"truth.json"}} == \
        {k: v for k, v in files["c"].items() if k not in layered | {"truth.json"}}
    tb, tc = (json.loads(files[d]["truth.json"]) for d in ("b", "c"))
    assert tb.pop("print_layers") == sha and set(sha) == {T_USAGE, T_LOADER} and "print_layers" not in tc and tb == tc
    for name in (T_USAGE, T_LOADER):
        tdir = site / "templates" / name
        tpl = Template(tdir / "template.yaml")
        assert tpl.print_path == tdir / "print.png" and tpl.uses_print_layer
        assert tpl.print_sha == sha[name] == printlayer.sha(imread_gray(tdir / "print.png"))
        blank = synth_usage.BUILDERS[name]()[0]
        assert not np.array_equal(imread_gray(tdir / "print.png"), blank)  # 생성기의 빈 그림이 아니다
        bp = grid.binarize(blank) > 0
        assert (printlayer.binary(tpl.print_layer) & bp).sum() / bp.sum() >= 0.99
        spec_b, spec_c = (yaml.safe_load(files[d][f"site/templates/{name}/template.yaml"]) for d in ("b", "c"))
        assert spec_b.pop("print_image") == "print.png" and spec_b == spec_c
    # 근무 시각 칸마다 인쇄된 "~" (칸 가운데) — 층의 마스크가 덮는다
    tpl = Template(site / "templates" / T_LOADER / "template.yaml")
    rng = [c for c in tpl.cells() if c.region == "shifts" and c.name == "range"]
    assert len(rng) == len(synth_usage.SHIFTS)
    for c in rng:
        x0, y0, x1, y1 = c.bbox
        mid = (grid.binarize(synth_usage.build_loader_log()[0]) > 0)[y0:y1, (x0 + x1) // 2 - 30:(x0 + x1) // 2 + 30]
        assert mid.sum() > 40 and tpl.print_mask[y0:y1, (x0 + x1) // 2 - 30:(x0 + x1) // 2 + 30][mid].all()


def test_synth_keeps_only_the_pages_the_layer_will_use(monkeypatch):
    """synth --print-layers 가 모아 두는 그림: 쪽이 붙을 때마다 고르지 않을 쪽을 버려도(_drop_unpicked) 마지막에 고르는 쪽은 다 모은
    뒤 고른 것과 같고, 모아 둔 그림은 MAX_PAGES 장을 넘지 않는다 (날수가 많아도 메모리가 늘지 않는다)."""
    import minedocscan.tools.printlayer as tp
    from minedocscan.tools.synth import _drop_unpicked, _layer_pick

    monkeypatch.setattr(tp, "MAX_PAGES", 17)                         # 첫 돌림 12장 + 둘째 돌림의 앞 5장
    per_day = [3, 1, 2, 4, 1, 3, 2, 2, 5, 1, 1, 3]                    # 날마다 쪽 수 (날짜순으로 붙는다)
    full: dict = {}
    stream: dict = {}
    for d, n in enumerate(per_day):
        day = f"2030-01-{d + 1:02d}"
        for i in range(n):
            full.setdefault(day, []).append((day, i))
            stream.setdefault(day, []).append((day, i))
            _drop_unpicked(stream)
            assert sum(x is not None for v in stream.values() for x in v) <= 17
    pick = _layer_pick(full)
    assert len(pick) == 17 and pick == _layer_pick(stream)
    assert [stream[d][i] for d, i in pick] == [full[d][i] for d, i in pick]
    assert sum(x is not None for v in stream.values() for x in v) == 17
    assert [d for d, i in pick if i == 0] == sorted(full) and [i for _d, i in pick].count(1) == 5
