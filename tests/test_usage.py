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
from minedocscan.review.server import ReviewApp
from minedocscan.review.store import Review, load, save
from minedocscan.tools import synth_meta
from minedocscan.tools.synth_usage import T_LOADER, T_USAGE
from minedocscan.validate.usage import TOLERANCE, check_usage
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
    # 계기 칸은 readings 대기열로: 쪽마다 세 칸을 한 번에 (검수 화면이 보내는 것과 같은 /api/reviews)
    app = ReviewApp(con, site, settings, "jp", "readings")
    key = {f["field_id"]: (f["source"], f["template_name"], f["region"], f["field_name"], f["row_key"]) for f in usage_fields(con)}
    q = app.queue_json({})
    states["readings"] = [q]
    n = 0
    for it in q["items"]:
        items = [{"field_id": c["field_id"], "verdict": "value" if answers.get(key[c["field_id"]]) else "empty",
                  "value": answers.get(key[c["field_id"]], "")} for c in it["cells"]]
        states["readings"].append(app.post_reviews({"items": items}))
        n += len(items)
    states["readings"].append(app.queue_json({}))
    n += review_usage(con, site, settings, answers, regions=("shifts", "fields"))
    f = con.execute("SELECT f.*, d.source_name || '#' || p.page_no AS source, p.template_name FROM doc_field f "
                    "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
                    "WHERE f.field_id = ?", (cell["tally_id"],)).fetchone()
    save(con, site, settings, Review(cell["tally_id"], "value",
                                     answers[(f["source"], f["template_name"], f["region"], f["field_name"], f["row_key"])], "jp"))
    n += 4
    states["checks"] = _checks(con)                     # 정답대로 검수한 뒤의 검산

    # 단계 4: 계기 값 하나를 고치면 → 그 쪽(총)과 그 다음 기록(연속성)의 검산이 바로 바뀐다. 마무리의 전체 계산과 같다
    usage = sorted((t for t in usage_synth.truth["usage"] if t["equipment"] == "LOADER"), key=lambda t: t["date"])
    first, second = usage[0], usage[1]
    end_fid = _by_source(con)[first["source"]]["end_field_id"]
    save(con, site, settings, Review(end_fid, "value", f"{first['meter_end'] + 0.5:.1f}", "jp"))
    states["meter_fixed"] = (_checks(con), _by_source(con), _full_recompute_equal(con, site))
    save(con, site, settings, Review(end_fid, "value", f"{first['meter_end']:.1f}", "jp"))
    # 장비명을 바꾸면 → 예전 장비와 새 장비 양쪽의 연속성이 다시 계산된다
    drill = min((t for t in usage_synth.truth["usage"] if t["equipment"] == "DRILL"), key=lambda t: t["date"])
    eq_fid = f"{_by_source(con)[drill['source']]['page_id']}:fields:equipment:-1"
    save(con, site, settings, Review(eq_fid, "value", "TRUCK", "jp"))
    states["renamed"] = (_checks(con), _full_recompute_equal(con, site))
    save(con, site, settings, Review(eq_fid, "value", "DRILL", "jp"))
    n += 4

    # 단계 5: usage-check — 고쳐서 맞으면 빠지고, 한 칸만 고쳐 여전히 어긋나면 남고, 고치지 않고 확인하면 끝난다.
    # 마지막에는 적힌 값으로 되돌린다 (합성 정답과 같아야 하므로)
    uc = ReviewApp(con, site, settings, "jp", "usage-check")
    states["uc"] = [uc.queue_json({})]
    items = {it["check"]["check_kind"] + ":" + it["check"]["result"]: it for it in states["uc"][0]["items"]}
    over, total, gap = items["continuity:overlap"], items["total:mismatch"], items["continuity:gap"]

    def post(item, values: dict) -> None:
        uc.post_reviews({"items": [{"field_id": fid, "verdict": "value", "value": v, "note": item["item_id"]}
                                   for fid, v in values.items()]})

    start, prev_end = over["cells"][0], over["cells"][1]
    post(over, {start["field_id"]: prev_end["current"]["value"]})             # 시작을 앞 기록의 종료로 고친다 → 맞는다
    states["uc"].append(uc.queue_json({}))
    post(over, {start["field_id"]: start["current"]["value"]})                # 적힌 값으로 되돌린다 → 다시 어긋난다
    states["uc"].append(uc.queue_json({}))
    end = next(c for c in total["cells"] if c["label"].startswith("계기 종료"))
    post(total, {end["field_id"]: f"{float(end['current']['value']) + 0.3:.1f}"})   # 한 칸만, 여전히 어긋난다 → 남는다
    states["uc"].append(uc.queue_json({}))
    post(total, {end["field_id"]: end["current"]["value"]})
    post(gap, {c["field_id"]: c["current"]["value"] for c in gap["cells"]})    # 고치지 않고 확인 → 끝난다 (gap 은 그대로)
    states["uc"].append(uc.queue_json({}))
    states["uc_ids"] = {"overlap": over["item_id"], "total": total["item_id"], "gap": gap["item_id"]}
    # 한 칸이 두 검산에 걸쳐 있다 (그 쪽의 종료 칸 = 총 검산 + 다음 기록의 연속성): 차례로 확인하면 둘 다 끝난다 (같은 초에 저장해도)
    shared = {c["field_id"] for c in total["cells"]} & {c["field_id"] for c in over["cells"]}
    post(total, {c["field_id"]: c["current"]["value"] for c in total["cells"]})
    post(over, {c["field_id"]: c["current"]["value"] for c in over["cells"]})
    states["uc"].append(uc.queue_json({}))
    states["uc_shared"] = shared
    n += 4 + len(gap["cells"]) + len(total["cells"]) + len(over["cells"])
    return {"con": con, "settings": settings, "site": site, "n": n, "root": root, "page_id": pid, "states": states,
            "tally_id": cell["tally_id"], "loader": (first["source"], second["source"]), "drill": drill["source"]}


def _checks(con) -> dict:
    return {(r["source"], r["check_kind"], r["item"]): dict(r) for r in con.execute(
        "SELECT x.*, d.source_name || '#' || p.page_no AS source FROM xcheck_usage x JOIN doc_page p ON x.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id")}


def _full_recompute_equal(con, site) -> bool:
    """검수 직후의 검산(그 쪽·그 장비만 다시 계산한 것) == 마무리 단계의 전체 계산."""
    other = clone_db(con)
    check_usage(other, site)
    return _dump(other, "xcheck_usage") == _dump(con, "xcheck_usage")


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


# ── 단계 4: 검산 ───────────────────────────────────────────────────────────
def test_checks_follow_the_scenarios(reviewed, usage_synth):
    """합성 정답대로 검수하면: 이어지는 장비는 match, 빠진 날은 gap 과 낀 날 수, 잘못 적은 시작은 overlap 과 차이, 첫 기록은 first,
    시각을 적은 장비는 연속성 검산이 없다. 총 ≠ 종료 − 시작 인 쪽은 그 검산만 mismatch, 소계가 틀린 행은 mismatch."""
    checks, truth = reviewed["states"]["checks"], usage_synth.truth["usage"]
    by = {t["source"]: t for t in truth}
    cont = {k[0]: v for k, v in checks.items() if k[1] == "continuity"}
    meters = sorted((t for t in truth if t["meter_start"] is not None), key=lambda t: (t["date"], t["meter_start"]))
    seen: dict[str, dict] = {}
    for t in meters:                                        # 같은 장비의 앞 기록 — 시험 안에서 따로 센다
        c, prev = cont[t["source"]], seen.get(t["equipment"])
        if prev is None:
            assert c["result"] == "first" and c["value_b"] is None, t["source"]
        else:
            d = round(t["meter_start"] - prev["meter_end"], 4)
            want = "match" if abs(d) <= TOLERANCE else ("gap" if d > 0 else "overlap")
            assert (c["result"], c["value_b"], c["other_page_id"]) == (want, prev["meter_end"], _pid(reviewed, prev)), t["source"]
            assert c["diff"] == pytest.approx(d)
        seen[t["equipment"]] = t
    results = {s: c["result"] for s, c in cont.items()}
    wrong = [t for t in truth if "TRUCK_wrong_start" in t["scenarios"]]
    assert wrong and all(results[t["source"]] == "overlap" and cont[t["source"]]["diff"] == pytest.approx(-2.0) for t in wrong)
    after = [t for t in truth if "DRILL_after_missing_day" in t["scenarios"]]
    assert after and all(results[t["source"]] == "gap" and cont[t["source"]]["days_between"] == 1 for t in after)
    two = [t for t in truth if "TRUCK_two_sheets" in t["scenarios"]]
    assert sorted(results[t["source"]] for t in two) == ["first", "match"]                # 시작 값이 작은 장이 먼저
    assert not any(by[s]["reading_kind"] in ("clock", "empty") for s in cont)              # 시각·빈 계기: 연속성 검산 없음
    totals = {k[0]: v for k, v in checks.items() if k[1] == "total"}
    assert {s: v["result"] for s, v in totals.items()} == {
        t["source"]: "mismatch" if "TRUCK_total_mismatch" in t["scenarios"] else "match"
        for t in truth if t["meter_total"] is not None}
    sub = [v for k, v in checks.items() if k[1] == "subtotal"]
    bad = [t for t in truth if "LOADER_subtotal_mismatch" in t["scenarios"]]
    assert sorted(v["source"] for v in sub if v["result"] == "mismatch") == [t["source"] for t in bad]
    assert all(v["result"] == "match" for v in sub if v["source"] not in {t["source"] for t in bad})
    # 검산은 값을 고치지 않는다: 어긋난 쪽의 값은 적힌 그대로 (가동 시간은 4.3 의 순서대로 — 종료 − 시작)
    rows = _by_source(reviewed["con"])
    for t in wrong + [t for t in truth if "TRUCK_total_mismatch" in t["scenarios"]]:
        u = rows[t["source"]]
        assert (u["meter_start"], u["meter_end"], u["hours_basis"]) == (t["meter_start"], t["meter_end"], "meter")
        assert u["hours"] == pytest.approx(t["meter_end"] - t["meter_start"])
    assert build_report(reviewed["con"])["xcheck_usage"]["continuity"]["gap"] == len(after)


def _pid(reviewed, t) -> str:
    return _by_source(reviewed["con"])[t["source"]]["page_id"]


def test_fixing_one_meter_value_moves_this_and_the_next_check(reviewed):
    checks, rows, same = reviewed["states"]["meter_fixed"]
    first, second = reviewed["loader"]
    assert same                                                   # 그 장비만 다시 계산한 것 == 전체 계산
    assert checks[(first, "total", "")]["result"] == "mismatch"   # 그 쪽: 총 = 종료 − 시작 이 어긋났다
    c = checks[(second, "continuity", "")]                        # 그 다음 기록: 시작 < 고친 종료
    assert c["result"] == "overlap" and c["diff"] == pytest.approx(-0.5)
    before = reviewed["states"]["checks"]
    assert before[(second, "continuity", "")]["result"] == "match" and before[(first, "total", "")]["result"] == "match"
    assert rows[second]["meter_start"] == _by_source(reviewed["con"])[second]["meter_start"]   # 다음 쪽의 값은 그대로


def test_renaming_moves_the_record_between_both_equipments(reviewed, usage_synth):
    """DRILL 의 첫 기록을 TRUCK 이라고 고치면: DRILL 에 남은 기록은 앞 기록이 없어지고(first), 옮겨 간 기록은 TRUCK 의 사슬에서
    그날 시작 값이 가장 작은 기록이 되어 TRUCK 의 주간 장이 그것과 비교된다 (계기 값이 크게 달라 gap)."""
    checks, same = reviewed["states"]["renamed"]
    before = reviewed["states"]["checks"]
    assert same
    truth = usage_synth.truth["usage"]
    drill_after = next(t["source"] for t in truth if "DRILL_after_missing_day" in t["scenarios"])
    assert before[(drill_after, "continuity", "")]["result"] == "gap"
    assert checks[(drill_after, "continuity", "")]["result"] == "first"              # 예전 장비
    moved = checks[(reviewed["drill"], "continuity", "")]
    assert moved["equipment_ref"] == f"id:{equipment_id('EQ-0501')}" and moved["result"] == "first"
    day0 = min(t["date"] for t in truth)
    truck_day = min((t for t in truth if t["equipment"] == "TRUCK" and t["date"] == day0), key=lambda t: t["meter_start"])
    assert before[(truck_day["source"], "continuity", "")]["result"] == "first"
    c = checks[(truck_day["source"], "continuity", "")]                             # 새 장비
    assert c["result"] == "gap" and c["other_page_id"] == _pid(reviewed, next(t for t in truth if t["source"] == reviewed["drill"]))


# ── 단계 5: 검수 대기열 ─────────────────────────────────────────────────────
def test_readings_queue_shows_one_page_at_a_time_without_machine_or_other_pages(reviewed, usage_synth):
    q0, *saves, q1 = reviewed["states"]["readings"]
    truth = usage_synth.truth["usage"]
    inked = [t for t in truth if t["reading_kind"] != "empty"]
    assert (q0["total"], q0["done"], len(q0["items"])) == (len(inked), 0, len(inked))
    assert (q1["total"], q1["done"], q1["items"]) == (len(inked), len(inked), [])
    for it in q0["items"]:
        pid = it["item_id"].split(":", 1)[1]
        assert [c["label"] for c in it["cells"]] == ["계기 시작", "계기 종료", "총"]
        assert all(c["field_id"].startswith(pid + ":meter:") for c in it["cells"])        # 다른 쪽(앞날)의 칸이 없다
        assert all(c["machine"] is None and c["human"] is None and c["current"] is None for c in it["cells"])
        assert {c["format"] for c in it["cells"]} == {"reading"}
    blob = json.dumps(q0, ensure_ascii=False)
    for t in truth:                                                    # 응답 어디에도 계기 값(정답)이 없다
        for v in (t["meter_start"], t["meter_end"], t["clock_start"]):
            assert v is None or (f"{v:.1f}" if isinstance(v, float) else v) not in blob
    # 한 쪽의 세 칸이 한 번의 저장으로 남는다 (같은 시각)
    assert all(len(s["saved"]) == 3 and s["reviewed_at"] for s in saves)
    recs = [rv for _seq, rv in load(reviewed["settings"].reviews)[0] if rv.region == "meter"]
    by_page: dict = {}
    for rv in recs[: 3 * len(saves)]:
        by_page.setdefault(rv.page_id, set()).add(rv.reviewed_at)
    assert len(by_page) == len(saves) and all(len(v) == 1 for v in by_page.values())


def test_readings_audit_samples_pages_whatever_the_ink(usage_run):
    con, site = clone_db(usage_run["pipe"].con), usage_run["pipe"].site
    q = build_queue(con, "readings", site=site, audit=6, seed=1)
    dates = {it["work_date"] for it in q["items"]}
    assert q["total"] == 6 and len(dates) == 3                          # 날짜별로 고르게
    assert build_queue(con, "readings", site=site, audit=6, seed=1)["items"] == q["items"]


def test_usage_check_fix_leaves_still_wrong_stays_confirm_finishes(reviewed):
    q0, fixed, restored, still, confirmed, both = reviewed["states"]["uc"]
    ids = reviewed["states"]["uc_ids"]
    item_ids = lambda q: {it["item_id"] for it in q["items"]}                              # noqa: E731
    bad = len(q0["items"])
    assert (q0["total"], q0["done"]) == (bad, 0) and bad >= 4                              # gap, overlap, 총, 소계
    assert ids["overlap"] not in item_ids(fixed) and (fixed["total"], fixed["done"]) == (bad, 1)      # 고쳐서 맞으면 빠진다
    assert ids["overlap"] in item_ids(restored) and (restored["total"], restored["done"]) == (bad, 0)
    assert ids["total"] in item_ids(still) and still["done"] == 0                           # 한 칸만 고쳤고 여전히 어긋난다
    assert ids["gap"] not in item_ids(confirmed) and confirmed["done"] == 1                 # 고치지 않고 확인했다
    assert ids["overlap"] in item_ids(confirmed) and ids["total"] in item_ids(confirmed)
    assert len(reviewed["states"]["uc_shared"]) == 1                                       # 종료 칸 하나를 같이 쓴다
    assert ids["overlap"] not in item_ids(both) and ids["total"] not in item_ids(both) and both["done"] == 3
    # 확인했어도 검산은 그대로 gap — 값을 맞춰 넣지 않는다
    con = reviewed["con"]
    pid = ids["gap"].split(":")[1]
    assert con.execute("SELECT result FROM xcheck_usage WHERE page_id = ? AND check_kind = 'continuity'", (pid,)).fetchone()[0] == "gap"
    for it in q0["items"]:                                                                  # 지금 값과 차이, 기계 값은 없다
        assert it["check"]["result"] in ("gap", "overlap", "mismatch")
        assert all(c["current"] is not None and c["machine"] is None for c in it["cells"])
    over = next(it for it in q0["items"] if it["item_id"] == ids["overlap"])
    assert len({c["field_id"].split(":")[0] for c in over["cells"]}) == 2                  # 두 쪽의 칸


def test_stats_count_by_format_and_by_queue(reviewed):
    from minedocscan.review.store import stats

    st = stats(reviewed["con"], reviewed["site"])
    assert st["by_format"]["reading"] > 0 and st["by_format"]["time_range"] > 0 and st["by_format"]["text"] > 0
    assert st["by_queue"]["readings"]["done"] == st["by_queue"]["readings"]["total"] > 0
    assert st["by_queue"]["usage-check"]["total"] >= 4
