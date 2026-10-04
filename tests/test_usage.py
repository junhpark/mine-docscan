"""장비 가동 일보 (tasks/0005 단계 3): usage 핸들러, eq_usage_daily·prod_tally, 장비명 대응표, equipment 키는 새 키일 뿐."""
from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pytest
import yaml

from conftest import clone_db, review_usage, usage_fields
from minedocscan.config import ConfigError
from minedocscan.evaluate.meta import _changed_pages
from minedocscan.forms.equipment import equipment_id
from minedocscan.forms.sitepack import SitePack
from minedocscan.forms.template import Template, TemplateError
from minedocscan.pipeline import Pipeline
from minedocscan.report import build_report
from minedocscan.review.queue import build_queue
from minedocscan.review.store import Review, load, save
from minedocscan.tools import synth_meta
from minedocscan.tools.synth_usage import T_LOADER, T_USAGE
from test_review_store import TABLES, _dump

USAGE_TABLES = TABLES


def _by_source(con) -> dict[str, dict]:
    return {r["source"]: dict(r) for r in con.execute(
        "SELECT u.*, d.source_name || '#' || p.page_no AS source FROM eq_usage_daily u JOIN doc_page p ON u.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id")}


def _tally(con) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in con.execute("SELECT t.*, d.source_name || '#' || p.page_no AS source FROM prod_tally t JOIN doc_page p "
                         "ON t.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id"):
        out.setdefault(r["source"], {})[(f"{r['item']}|{r['place']}", r["column_name"])] = dict(r)
    return out


UNREAD = ("meter", "shifts", "fields")          # 정답 인식기(oracle)도 읽지 않는 칸: 소수·시각, 표 밖의 장비명·운전자


@pytest.fixture(scope="module")
def reviewed(usage_run, usage_synth, tmp_path_factory) -> dict:
    """usage_run 의 복사본에 사람의 일을 한다 (검수 파일은 따로):
      1) LOADER 쪽 하나의 장비명을 SHOVEL 로, 다시 대응표에 없는 이름으로 → 그때마다의 업무 행을 적어 둔다
      2) 작업량 칸 하나를 틀린 값으로 → 적어 둔다
      3) 읽지 않는 칸(계기·근무 시각·장비명·운전자) 전부와 2) 의 칸을 정답대로 — 나중 검수가 이긴다
    마지막 상태는 합성 정답과 같아야 하고, 같은 검수 파일로 새로 돌린 DB 와 같아야 한다 (불변식)."""
    root = tmp_path_factory.mktemp("usage_reviewed")
    con = clone_db(usage_run["pipe"].con)
    settings = replace(usage_run["settings"], reviews=root / "reviews.jsonl")
    site, answers = usage_run["pipe"].site, usage_run["answers"]
    loader = next(t for t in usage_synth.truth["usage"] if t["equipment"] == "LOADER" and t["tally"])
    pid = _by_source(con)[loader["source"]]["page_id"]
    states = {}
    for name in ("SHOVEL", " shovel 2 "):
        save(con, site, settings, Review(f"{pid}:fields:equipment:-1", "value", name, "jp"))
        states[name] = (dict(con.execute("SELECT * FROM eq_usage_daily WHERE page_id = ?", (pid,)).fetchone()),
                        {(r["equipment"], r["equipment_id"]) for r in con.execute("SELECT * FROM prod_tally WHERE page_id = ?",
                                                                                   (pid,))})
    cell = con.execute("SELECT * FROM prod_tally WHERE page_id = ? AND has_value = 1 ORDER BY tally_id", (pid,)).fetchone()
    save(con, site, settings, Review(cell["tally_id"], "value", str(cell["count"] + 10), "jp"))
    states["tally"] = dict(con.execute("SELECT * FROM prod_tally WHERE tally_id = ?", (cell["tally_id"],)).fetchone())
    n = review_usage(con, site, settings, answers, regions=UNREAD)
    f = con.execute("SELECT f.*, d.source_name || '#' || p.page_no AS source, p.template_name FROM doc_field f "
                    "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
                    "WHERE f.field_id = ?", (cell["tally_id"],)).fetchone()
    save(con, site, settings, Review(cell["tally_id"], "value",
                                     answers[(f["source"], f["template_name"], f["region"], f["field_name"], f["row_key"])], "jp"))
    return {"con": con, "settings": settings, "site": site, "n": n + 4, "root": root, "page_id": pid, "states": states,
            "tally_id": cell["tally_id"]}


# ── 기계만으로: 읽는 칸과 읽지 않는 칸 ──────────────────────────────────────
def test_machine_run_reads_integers_and_leaves_meters_to_people(usage_run, usage_synth):
    con, truth = usage_run["pipe"].con, usage_synth.truth
    pages = {r["source"]: r for r in con.execute("SELECT d.source_name || '#' || p.page_no AS source, p.status, p.template_name "
                                                 "FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id")}
    assert len(pages) == len(truth["usage"]) and {p["status"] for p in pages.values()} == {"loaded"}
    assert all(pages[t["source"]]["template_name"] == t["template"] for t in truth["usage"])
    rows, tally = _by_source(con), _tally(con)
    assert len(rows) == len(truth["usage"])                              # 쪽 하나에 한 행
    for t in truth["usage"]:
        u = rows[t["source"]]
        # 소수·시각은 읽지 않는다: 계기 칸에 잉크가 있으면 모르는 칸(pending), 가동 시간은 아직 모른다
        assert u["reading_kind"] == ("empty" if t["reading_kind"] == "empty" else "pending"), t["source"]
        assert u["hours"] is None and u["meter_start"] is None and u["meter_start_raw"] is None
        assert u["review_status"] == ("auto" if t["reading_kind"] == "empty" and not t["shifts"] else "pending")
        # 잉크로 정하는 것은 검수 없이도 맞다: 작업 표의 글씨 있는 줄, 서명
        assert (u["activity_rows"], u["signed"]) == (t["activity_rows"], t["signed"]), t["source"]
        assert u["equipment"] is None and u["equipment_id"] is None      # 라벨도 모델도 없다 → page-fields 대기열
        # 작업량 표의 정수 칸은 숫자 인식 경로를 그대로 탄다 (oracle: 읽은 값 = 정답). 표 위의 메모는 값이 아니다
        got = {k: (r["count_raw"], r["count"]) for k, r in tally.get(t["source"], {}).items() if r["has_value_raw"]}
        assert got == {(c["row_key"], c["column"]): (c["count"], c["count"]) for c in t["tally"]}, t["source"]
    meter = con.execute("SELECT * FROM doc_field WHERE region IN ('meter', 'shifts') AND has_value_raw = 1").fetchall()
    assert meter and all(f["value_raw"] is None and f["backend"] == "ink" and f["status_raw"] == "pending" for f in meter)
    assert {f["format"] for f in meter} == {"reading", "time_range"}
    assert all(r["is_subtotal"] == (k[1] == "sub") for cells in tally.values() for k, r in cells.items())
    memo = next(t for t in truth["usage"] if "LOADER_memo_on_tally" in t["scenarios"])
    assert usage_run["pipe"].summary["handlers"]["usage"]["notes"] >= 1 and memo["tally"] is not None


# ── 검수로 정답을 넣으면 ───────────────────────────────────────────────────
def test_review_makes_usage_and_tally_equal_the_truth(reviewed, usage_synth):
    con = reviewed["con"]
    rows, tally = _by_source(con), _tally(con)
    for t in usage_synth.truth["usage"]:
        u = rows[t["source"]]
        want_id = equipment_id(t["alias_key"]) if t["alias_key"] else None
        assert (u["equipment"], u["equipment_id"], u["operator"]) == (t["equipment"], want_id, t["operator"]), t["source"]
        for k in ("reading_kind", "meter_start", "meter_end", "meter_total", "clock_start", "clock_end", "shift_minutes",
                  "activity_rows", "signed", "hours_basis"):
            assert u[k] == t[k], (t["source"], k)
        assert u["hours"] == pytest.approx(t["hours"]) if t["hours"] is not None else u["hours"] is None
        assert (json.loads(u["shifts"]) if u["shifts"] is not None else None) == t["shifts"], t["source"]
        assert u["review_status"] != "pending"
        got = {k: r["count"] for k, r in tally.get(t["source"], {}).items() if r["has_value"]}
        assert got == {(c["row_key"], c["column"]): c["count"] for c in t["tally"]}, t["source"]
    # 기계 값은 검수가 건드리지 않는다
    assert con.execute("SELECT COUNT(*) FROM eq_usage_daily WHERE meter_start_raw IS NOT NULL").fetchone()[0] == 0
    t = con.execute("SELECT * FROM prod_tally WHERE tally_id = ?", (reviewed["tally_id"],)).fetchone()
    assert reviewed["states"]["tally"]["count"] == t["count"] + 10 and reviewed["states"]["tally"]["count_raw"] == t["count_raw"]


def test_renaming_the_equipment_rebuilds_the_page(reviewed):
    """장비명(메타 필드)의 검수도 그 쪽의 업무 행을 다시 만든다 — eq_usage_daily 와 prod_tally 의 장비·ID."""
    (u1, t1), (u2, t2) = reviewed["states"]["SHOVEL"], reviewed["states"][" shovel 2 "]
    assert (u1["equipment"], u1["equipment_id"]) == ("SHOVEL", equipment_id("EQ-0401")) and t1 == {("SHOVEL", u1["equipment_id"])}
    assert (u2["equipment"], u2["equipment_id"]) == ("shovel 2", None) and t2 == {("shovel 2", None)}   # 비슷한 이름으로 맞추지 않는다


def test_clock_pages_nothing_written_two_sheets_and_unknown_names(reviewed, usage_synth):
    con, truth = reviewed["con"], usage_synth.truth["usage"]
    rows = _by_source(con)
    clock = [rows[t["source"]] for t in truth if "PUMP_clock_unaliased" in t["scenarios"]]
    assert clock and all(u["meter_start"] is None and u["meter_end"] is None and u["clock_start"] and u["clock_end"]
                         and u["hours_basis"] == "clock" for u in clock)
    # 점으로 쓴 시각(08.00)도 사람이 콜론으로 입력한 대로 남는다
    assert all(":" in u["clock_start"] and ":" in u["clock_end"] for u in clock)
    # 대응표에 없는 장비명: ID 는 NULL, 이름은 남는다
    assert all(u["equipment"] == "PUMP" and u["equipment_id"] is None for u in clock)
    nothing = [rows[t["source"]] for t in truth if "SHOVEL_nothing_written" in t["scenarios"]]
    assert nothing and all(u["hours"] is None and u["hours_basis"] is None and u["reading_kind"] == "empty" for u in nothing)
    shifts_only = [rows[t["source"]] for t in truth if "SHOVEL_shifts_only" in t["scenarios"]]
    assert shifts_only and all(u["hours_basis"] == "shifts" and u["shift_minutes"] > 0 for u in shifts_only)
    # 하루에 두 장을 낸 장비는 두 행이다 (합치지 않는다)
    day0 = usage_synth.truth["usage"][0]["date"]
    trucks = [u for u in rows.values() if u["equipment"] == "TRUCK" and u["work_date"] == day0]
    assert len(trucks) == 2 and len({u["page_id"] for u in trucks}) == 2
    assert {u["operator"] for u in trucks} == {"CHARLIE", "DELTA"}
    rep = build_report(con)["usage"]
    assert rep["pages"] == len(truth) and rep["with_equipment_id"] == sum(t["alias_key"] is not None for t in truth)
    assert rep["hours_basis"] == {k: sum((t["hours_basis"] or "none") == k for t in truth) for k in rep["hours_basis"]}


def test_review_file_rebuilds_the_same_db(reviewed, usage_run, usage_synth, tmp_path):
    """불변식: 검수를 저장한 직후의 DB = 같은 검수 파일로 새로 돌린 DB (새 테이블 포함). 같은 칸의 여러 검수, 장비명 바꾸기 포함."""
    fresh = Pipeline(replace(reviewed["settings"], work_root=tmp_path / "w"), recognizer=usage_run["pipe"].recognizer)
    fresh.run([usage_synth.scans])
    assert fresh.summary["reviews"]["imported"] == reviewed["n"]
    assert build_report(fresh.con) == build_report(reviewed["con"])
    for t in USAGE_TABLES:
        assert _dump(fresh.con, t) == _dump(reviewed["con"], t), t


def test_bad_meter_input_is_refused_before_the_file(reviewed, tmp_path):
    con = clone_db(reviewed["con"])
    settings = replace(reviewed["settings"], reviews=tmp_path / "r.jsonl")
    f = next(f for f in usage_fields(con) if f["region"] == "meter")
    from minedocscan.forms.formats import FormatError

    for bad in ("12:75", "1234,5", "8~9"):
        with pytest.raises(FormatError):
            save(con, reviewed["site"], settings, Review(f["field_id"], "value", bad, "jp"))
    assert not settings.reviews.exists()
    save(con, reviewed["site"], settings, Review(f["field_id"], "value", "0800", "jp"))     # 숫자: 계기 값 800 (콜론이 없다)
    save(con, reviewed["site"], settings, Review(f["field_id"], "value", "8:00", "jp"))
    assert [rv.value for _s, rv in load(settings.reviews)[0]] == ["800", "08:00"]


# ── 템플릿과 사이트 팩 ─────────────────────────────────────────────────────
def _usage_tpl(tmp_path, mutate) -> Template:
    from minedocscan.tools.synth_usage import build_loader_log

    _img, spec = build_loader_log()
    mutate(spec)
    p = tmp_path / "t" / "template.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return Template(p)


def _region(spec, name):
    return next(r for r in spec["regions"] if r["name"] == name)


def test_template_roles_splits_and_subtotals(tmp_path):
    t = _usage_tpl(tmp_path, lambda s: None)
    tally = [c for c in t.cells() if c.region == "tally" and c.name == "a"]
    assert len(tally) == 14 and tally[0].bbox[3] - tally[0].bbox[1] == 44 - 8     # 나눔 선으로 두 줄 (인쇄된 괘선 없이)
    assert t.region("tally")["grid"]["ys"] == sorted(set(t.region("tally")["grid"]["ys"]))    # 정합은 괘선만 본다
    cases = [
        (lambda s: _region(s, "meter").update(role="gauge"), "알 수 없는 role"),
        (lambda s: _region(s, "meter")["columns"].pop(1), "필요한 칸이 없습니다: end"),
        (lambda s: _region(s, "meter")["columns"][0].update(format="integer"), "role meter 의 칸 형식"),
        (lambda s: _region(s, "shifts")["columns"][1].pop("format"), "time_range"),
        (lambda s: _region(s, "shifts")["columns"][0].update(subtotal=True), "subtotal 은 role tally"),
        (lambda s: _region(s, "tally")["columns"][4].update(subtotal="yes"), "true/false"),
        (lambda s: _region(s, "tally")["grid"].update(split_ys=[_region(s, "tally")["grid"]["ys"][2]]), "split_ys"),
        (lambda s: s["regions"].append(dict(_region(s, "meter"), name="meter2")), "role meter 인 표가 2개"),
        (lambda s: s.update(handler="haul"), "usage 핸들러의 표에만"),
    ]
    for i, (mutate, msg) in enumerate(cases):
        with pytest.raises(TemplateError, match=msg):
            _usage_tpl(tmp_path / str(i), mutate)
    # 세로로 놓인 계기 칸 (행 키 start·end·total, 손으로 쓰는 열 하나)도 된다
    def vertical(s):
        m = _region(s, "meter")
        m["columns"] = [{"idx": 0, "name": "value", "kind": "handwritten_number", "format": "reading"}]
        m["grid"] = {"ys": [700, 742, 784, 826], "xs": [1200, 1570]}
        m["header_rows"] = 0
        m["rows"] = [{"row": i, "key": k} for i, k in enumerate(("start", "end", "total"))]
    assert _usage_tpl(tmp_path / "v", vertical).region("meter")["rows"][2]["key"] == "total"


def test_equipment_aliases_must_point_into_the_master(usage_synth, tmp_path):
    site = SitePack(usage_synth.site)
    assert site.equipment_id_of("LOADER") == equipment_id("EQ-0301")
    assert site.equipment_id_of(" LOADER ") == equipment_id("EQ-0301")       # 앞뒤 빈칸만
    assert site.equipment_id_of("loader") is None and site.equipment_id_of("PUMP") is None and site.equipment_id_of(None) is None
    for i, extra in enumerate(('"GHOST" = "EQ-9999"', '"GHOST" = 3')):
        d = tmp_path / str(i)
        shutil.copytree(usage_synth.site, d)
        toml = (d / "site.toml").read_text(encoding="utf-8").replace('"DRILL" = "EQ-0201"', f'"DRILL" = "EQ-0201"\n{extra}')
        (d / "site.toml").write_text(toml, encoding="utf-8")
        with pytest.raises(ConfigError, match="5번째 항목") as e:
            SitePack(d)
        assert "GHOST" not in str(e.value) and "EQ-9999" not in str(e.value)         # 이름·장비 키를 찍지 않는다


# ── equipment 는 새 키일 뿐이다 ────────────────────────────────────────────
def test_equipment_key_goes_through_page_fields_and_exports(usage_run, tmp_path):
    from minedocscan.review.export import export_meta_crops

    con, site, settings = clone_db(usage_run["pipe"].con), usage_run["pipe"].site, usage_run["settings"]
    q = build_queue(con, "page-fields", site=site)
    assert q["total"] == len(_by_source(con)) and q["done"] == 0
    assert {c["meta_key"] for it in q["items"] for c in it["cells"]} == {"equipment", "operator"}
    assert {"LOADER", "SHOVEL", "TRUCK", "DRILL"} <= set(q["candidates"]["equipment"])        # 대응표의 이름
    assert "PUMP" not in q["candidates"]["equipment"]
    # 검수로 넣은 값은 후보에 더해지고, 그 쪽은 대기열에서 빠진다
    settings = replace(settings, reviews=tmp_path / "r.jsonl")
    it = q["items"][0]
    for c in it["cells"]:
        save(con, site, settings, Review(c["field_id"], "value", "PUMP" if c["meta_key"] == "equipment" else "KILO", "jp"))
    q2 = build_queue(con, "page-fields", site=site)
    assert q2["done"] == 1 and "PUMP" in q2["candidates"]["equipment"]
    out = export_meta_crops(con, site, settings, tmp_path / "crops", res="source")
    assert out["by_key"] == {"equipment": 1, "operator": 1}
    labels = [json.loads(x) for x in (tmp_path / "crops").rglob("labels.jsonl").__next__().read_text().splitlines()]
    assert {x["meta_key"] for x in labels} == {"equipment", "operator"}


def test_changed_pages_pair_every_key_with_the_writer():
    """배차가 바뀐 쪽: 작성자의 평소 값과 다른 차량번호든 장비명이든 (키 이름을 고르지 않는다)."""
    human = {("p1", "operator"): "ALPHA", ("p1", "equipment"): "LOADER", ("p2", "operator"): "ALPHA",
             ("p2", "equipment"): "LOADER", ("p3", "operator"): "ALPHA", ("p3", "equipment"): "TRUCK",
             ("p4", "operator"): "BRAVO", ("p4", "vehicle_no"): "4127", ("p5", "operator"): "BRAVO",
             ("p5", "vehicle_no"): "4127", ("p6", "operator"): "BRAVO", ("p6", "vehicle_no"): "5260",
             ("p6", "date.day"): "7", ("p7", "date"): "2030-01-07"}
    assert _changed_pages(human) == {"p3", "p6"}


def test_synthetic_meta_crops_know_the_equipment_key(tmp_path):
    out = synth_meta.write_meta_crops(tmp_path, ("equipment",), 1, writers=("ALPHA", "BRAVO"))
    lines = [json.loads(x) for x in (tmp_path / "train" / "meta" / "labels.jsonl").read_text().splitlines()]
    assert out["by_key"] == {"equipment": 2} and {x["text"] for x in lines} == {"LOADER", "SHOVEL"}
    with pytest.raises(ValueError, match="합성 값이 없는 키"):
        synth_meta.write_meta_crops(tmp_path / "x", ("weight",), 1)


def test_default_synth_has_no_usage(synth, site):
    assert "usage" not in synth.truth
    assert not {T_USAGE, T_LOADER} & set(site.templates) and site.equipment_aliases == {}
