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
from minedocscan.handlers.usage import UsageHandler
from minedocscan.pipeline import Pipeline
from minedocscan.recognize.builtin import OracleRecognizer
from minedocscan.report import build_report
from minedocscan.review.queue import build_queue
from minedocscan.review.server import ReviewApp
from minedocscan.review.store import Review, load, save
from minedocscan.tools import synth_meta, synth_usage
from minedocscan.tools.synth_usage import PRINTED_ITEMS, T_LOADER, T_USAGE
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
    cell = con.execute("SELECT * FROM prod_tally WHERE page_id = ? AND count IS NOT NULL ORDER BY tally_id", (pid,)).fetchone()
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
    n += _review_pending_tally(con, site, settings, answers)          # 잉크는 있는데 답이 없는 작업량 칸 (메모, 칸 안의 인쇄)
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


def _review_pending_tally(con, site, settings, answers) -> int:
    """검수 대기인 작업량 칸을 정답대로 (pending 대기열에서 사람이 하는 일)."""
    key = {f["field_id"]: (f["source"], f["template_name"], f["region"], f["field_name"], f["row_key"] or "")
           for f in usage_fields(con)}
    pend = [r["field_id"] for r in con.execute("SELECT field_id FROM doc_field WHERE region = 'tally' AND review_status = 'pending' "
                                               "ORDER BY field_id")]
    for fid in pend:
        text = answers.get(key[fid])
        save(con, site, settings, Review(fid, "value", text, "jp") if text else Review(fid, "empty", reviewer="jp"))
    return len(pend)


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
        # 작업량 표의 정수 칸은 숫자 인식 경로를 그대로 탄다 (oracle: 읽은 값 = 정답). 잉크는 있는데 답이 없는 칸(표 위의 메모,
        # 칸 안의 인쇄만)은 값 없이 검수 대기 — 빈 칸으로 자동 적재하지 않는다
        cells = tally.get(t["source"], {})
        got = {k: (r["count_raw"], r["count"]) for k, r in cells.items() if r["count_raw"] is not None}
        assert got == {(c["row_key"], c["column"]): (c["count"], c["count"]) for c in t["tally"]}, t["source"]
        assert all(r["review_status"] == "pending" for r in cells.values() if r["has_value_raw"] and r["count_raw"] is None)
    meter = con.execute("SELECT * FROM doc_field WHERE region IN ('meter', 'shifts') AND has_value_raw = 1").fetchall()
    assert meter and all(f["value_raw"] is None and f["backend"] == "ink" and f["status_raw"] == "pending" for f in meter)
    assert {f["format"] for f in meter} == {"reading", "time_range"}
    assert all(r["is_subtotal"] == (k[1] == "sub") for cells in tally.values() for k, r in cells.items())
    memo = next(t for t in truth["usage"] if "LOADER_memo_on_tally" in t["scenarios"])
    under = {k: r for k, r in tally[memo["source"]].items() if k[0] == "ROCK|YARD"}           # 메모가 지나간 행 (값이 없다)
    assert under and all(r["count"] is None for r in under.values())                          # 메모는 값이 되지 않는다
    assert any(r["has_value_raw"] and r["review_status"] == "pending" for r in under.values())  # 잉크 비율로는 "있음" → 사람이
    assert usage_run["pipe"].summary["handlers"]["usage"]["notes"] >= 1


def _printed_cells(con) -> dict:
    """칸 안에 라벨·단위가 인쇄된 작업량 칸 (PRINTED_ITEMS 의 행): (출처, 행 키, 열) → doc_field 행."""
    return {(r["source"], r["row_key"], r["field_name"]): dict(r) for r in con.execute(
        "SELECT f.*, d.source_name || '#' || p.page_no AS source FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id WHERE f.region = 'tally' AND f.kind LIKE 'handwritten%'")
        if r["row_key"].split("|")[0] in PRINTED_ITEMS}


def test_digits_between_printed_labels_are_never_auto_emptied(usage_run, usage_synth, tmp_path, monkeypatch):
    """작업량 칸 안에 라벨·단위가 인쇄된 행 (실제 로우더 작업일보의 "하단: _ 대"): 그 사이에 쓴 두 자리 숫자는 양옆의 인쇄와,
    인쇄는 이웃 칸의 인쇄와 이어져 덩어리 배정에서는 줄 전체가 메모가 된다. 잉크 비율로도 보므로(둘 중 하나라도 "있음")
    그 칸은 빈 칸으로 자동 적재되지 않고 인식기에 간다. 인쇄만 있는 칸은 답이 없으면 검수 대기."""
    truth = {(t["source"], c["row_key"], c["column"]): c["count"] for t in usage_synth.truth["usage"] for c in t["tally"]}
    written = {k: v for k, v in truth.items() if k[1].split("|")[0] in PRINTED_ITEMS}
    two = {k: v for k, v in written.items() if v >= 10}
    assert len(two) >= 6                                                     # 합성 묶음에 두 자리 값이 있다
    cells = _printed_cells(usage_run["pipe"].con)
    assert cells and set(written) <= set(cells)
    assert not [k for k, f in cells.items() if not f["has_value_raw"] and f["status_raw"] == "auto"]   # 빈 칸 자동 적재 없음
    assert {k: (f["value_raw"], f["status_raw"]) for k, f in cells.items() if k in written} == \
        {k: (str(v), "auto") for k, v in written.items()}                    # 쓴 값은 인식기가 읽는다 (oracle)
    assert all(f["status_raw"] == "pending" and f["value_final"] is None for k, f in cells.items() if k not in written)

    # 덩어리 배정만으로 보면(잉크 비율을 끄면) 두 자리 값 일부가 빈 칸으로 자동 적재된다 — 이 시험이 막는 실패가 합성 양식에 있다
    monkeypatch.setattr(UsageHandler, "text_ink_min", float("inf"))
    settings = replace(usage_run["settings"], work_root=tmp_path / "work", reviews=tmp_path / "reviews.jsonl")
    pipe = Pipeline(settings, recognizer=OracleRecognizer(usage_run["answers"]))
    pipe.run([usage_synth.scans])
    old = _printed_cells(pipe.con)
    lost = [k for k in two if not old[k]["has_value_raw"] and old[k]["status_raw"] == "auto"]
    assert lost, "덩어리 배정만으로도 다 잡힌다 — 합성 양식이 실제의 실패를 재현하지 못한다"


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
    want: dict = {}
    for t in truth:
        want[t["hours_basis"] or "none"] = want.get(t["hours_basis"] or "none", 0) + 1
    assert rep["hours_basis"] == want and rep["reading"] == {k: sum(t["reading_kind"] == k for t in truth)
                                                            for k in {t["reading_kind"] for t in truth}}


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


def test_equipment_alias_hash_is_canonical(usage_synth, tmp_path):
    """info 의 대응표 해시: sha256(정렬한 (이름, 장비 키) 쌍의 JSON, 구분자 "," ":")의 앞 16자. 항목의 순서·주석에는 그대로,
    한 이름을 다른 장비 키로 바꾸면 달라진다 (이름만·개수만 보는 해시가 아니다)."""
    import hashlib

    site = SitePack(usage_synth.site)
    pairs = sorted(site.equipment_aliases.items())
    canon = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    assert site.equipment_aliases_sha == hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    toml = (usage_synth.site / "site.toml").read_text(encoding="utf-8")
    block = toml[toml.index("[equipment.aliases]"):]
    lines = [x for x in block.splitlines()[1:] if x.startswith('"')]
    assert len(lines) == 4
    d = tmp_path / "reordered"                                      # 순서를 뒤집고 주석을 더한다
    shutil.copytree(usage_synth.site, d)
    (d / "site.toml").write_text(toml.replace(block, "[equipment.aliases]\n# 다른 주석\n" + "\n".join(reversed(lines)) + "\n"),
                                 encoding="utf-8")
    assert SitePack(d).equipment_aliases_sha == site.equipment_aliases_sha
    d2 = tmp_path / "remapped"                                      # 같은 이름, 다른 장비 키
    shutil.copytree(usage_synth.site, d2)
    (d2 / "site.toml").write_text(toml.replace('"LOADER" = "EQ-0301"', '"LOADER" = "EQ-0401"'), encoding="utf-8")
    assert SitePack(d2).equipment_aliases_sha != site.equipment_aliases_sha


def test_report_flags_equipment_ids_left_stale_by_an_alias_change(usage_synth, usage_run, reviewed, tmp_path, capsys,
                                                                    monkeypatch):
    """[equipment.aliases] 를 고친 뒤 (tasks/0006 단계 1): info 의 대응표 해시가 바뀌고, report 가 지금의 대응표와 장비 ID 가 다른
    행을 한 줄로 센다 (이름·장비 키 없이). 그 문서들을 다시 돌리면 사라진다. 세는 수는 build_report 의 dict 밖이다 (regress).
    장비명은 검수로 정해지므로(합성에는 라벨이 없다) 검수까지 한 DB(reviewed)에서 시작하고, 다시 돌릴 때도 같은 검수 파일을 쓴다."""
    import sqlite3

    from minedocscan.cli import main
    from minedocscan.report import stale_equipment_ids

    site, work = tmp_path / "site", tmp_path / "w"
    shutil.copytree(usage_synth.site, site)
    work.mkdir()
    out = sqlite3.connect(work / "minedocscan.db")                 # 픽스처의 DB 를 파일로 복사 — report 가 연다
    reviewed["con"].backup(out)
    out.close()
    common = ["--site", str(site), "--work-root", str(work)]

    def report() -> tuple[dict, str]:
        assert main(["report", "--json", *common]) == 0
        js = json.loads(capsys.readouterr().out)
        assert main(["report", *common]) == 0
        return js, capsys.readouterr().out

    def info() -> tuple[dict, str]:
        assert main(["info", "--json", *common]) == 0
        js = json.loads(capsys.readouterr().out)["site"]
        assert main(["info", *common]) == 0
        return js, capsys.readouterr().out

    zero = {"eq_usage_daily": 0, "prod_tally": 0, "pages": 0, "documents": 0}
    js, text = report()
    assert js["stale_equipment_ids"] == zero and "stale_equipment_ids" not in js["report"]
    assert "[equipment.aliases]" not in text
    i0, _ = info()
    assert i0["equipment_aliases"] == 4 and len(i0["equipment_aliases_sha"]) == 16
    # 대응표에서 DRILL 을 뺀다 — DRILL 은 첫날·셋째 날 문서에만 있다
    toml = (site / "site.toml").read_text(encoding="utf-8")
    assert '"DRILL" = "EQ-0201"\n' in toml
    (site / "site.toml").write_text(toml.replace('"DRILL" = "EQ-0201"\n', ""), encoding="utf-8")
    i1, itext = info()
    assert i1["equipment_aliases"] == 3 and i1["equipment_aliases_sha"] != i0["equipment_aliases_sha"]
    assert f"해시 {i1['equipment_aliases_sha']}" in itext
    docs = sorted(d for d, pages in usage_synth.truth["documents"].items() if any(p["equipment"] == "DRILL" for p in pages))
    assert len(docs) == 2
    js, text = report()
    st = js["stale_equipment_ids"]
    assert st == {"eq_usage_daily": 2, "prod_tally": 0, "pages": 2, "documents": 2}, st
    lines = [x for x in text.splitlines() if "[equipment.aliases]" in x]
    assert len(lines) == 1 and "가동 기록 2행" in lines[0] and "문서 2건" in lines[0] and "--fresh" in lines[0]
    for name in ("DRILL", "EQ-0201", "LOADER", "EQ-0301"):          # 이름·장비 키를 찍지 않는다
        assert name not in text and name not in itext and name not in json.dumps(js["stale_equipment_ids"])
    # 작업량(prod_tally)도 센다: LOADER 를 다른 장비 키로 바꾼 사이트 팩 (다시 돌리지 않고 세기만)
    site2 = tmp_path / "site2"
    shutil.copytree(site, site2)
    (site2 / "site.toml").write_text((site2 / "site.toml").read_text(encoding="utf-8").replace(
        '"LOADER" = "EQ-0301"', '"LOADER" = "EQ-0401"'), encoding="utf-8")
    con = sqlite3.connect(work / "minedocscan.db")
    st2 = stale_equipment_ids(con, SitePack(site2))
    con.close()
    n_loader = len(usage_synth.truth["documents"])                  # LOADER 는 날마다 한 쪽
    assert st2["eq_usage_daily"] == 2 + n_loader and st2["prod_tally"] > 0 and st2["documents"] == n_loader
    # 사이트 팩을 읽을 수 없으면 이 검사만 건너뛴다 (report 는 나온다): 마스터 밖의 장비 키, 깨진 파일명 정규식, dict 가 아닌 필드
    toml = (site / "site.toml").read_text(encoding="utf-8")
    broken = {"ConfigError": lambda d: (d / "site.toml").write_text(toml + '"GHOST" = "EQ-9999"\n', encoding="utf-8"),
              "error": lambda d: (d / "site.toml").write_text(toml.replace(
                  "date_from_filename = '", "date_from_filename = '(?P<yyyy"), encoding="utf-8")}

    def non_dict_field(d):
        p = d / "templates" / T_LOADER / "template.yaml"
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        spec["fields"].append("oops")
        p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")

    broken["AttributeError"] = non_dict_field
    for exc, breaker in broken.items():
        bad = tmp_path / f"bad-{exc}"
        shutil.copytree(site, bad)
        breaker(bad)
        args = ["--site", str(bad), "--work-root", str(work)]
        assert main(["report", "--json", *args]) == 0
        out = capsys.readouterr().out
        assert "stale_equipment_ids" not in json.loads(out) and "GHOST" not in out, exc
        assert main(["report", *args]) == 0
        text = capsys.readouterr().out
        notes = [x for x in text.splitlines() if "장비 ID 검사를 건너뛰었습니다" in x]
        assert len(notes) == 1 and notes[0].endswith(f": {exc})") and "GHOST" not in text and "EQ-9999" not in text, exc
        assert "[equipment.aliases]" not in text, exc
    # 사이트 팩이 없으면 이 검사만 건너뛴다 (환경변수·현재 폴더의 설정 파일이 사이트 팩을 대지 않게)
    monkeypatch.delenv("MINEDOCSCAN_SITE", raising=False)
    monkeypatch.delenv("MINEDOCSCAN_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["report", "--json", "--work-root", str(work)]) == 0
    assert "stale_equipment_ids" not in json.loads(capsys.readouterr().out)
    assert main(["report", "--work-root", str(work)]) == 0
    assert "장비 ID 검사" not in (t := capsys.readouterr().out) and "[equipment.aliases]" not in t
    con = sqlite3.connect(work / "minedocscan.db")
    before = con.execute("SELECT COUNT(*), COUNT(equipment), COUNT(equipment_id) FROM eq_usage_daily").fetchone()
    con.close()
    # 그 문서들만 다시 돌리면 사라진다
    settings = replace(reviewed["settings"], site=site, work_root=work)
    pipe = Pipeline(settings, recognizer=OracleRecognizer(usage_run["answers"]))
    for d in docs:
        pipe.process_file(usage_synth.scans / f"{d}.pdf")
    pipe.finalize()
    pipe.con.close()
    js, text = report()
    assert js["stale_equipment_ids"] == zero and "[equipment.aliases]" not in text
    # 장비 ID 를 다시 정했을 뿐 검수로 정한 장비명은 그대로다 (이름을 잃어 None == None 이 된 것이 아니다): 행 수·이름 수는 같고,
    # 대응표에서 뺀 장비의 두 쪽만 ID 가 NULL 이 되었다
    con = sqlite3.connect(work / "minedocscan.db")
    after = con.execute("SELECT COUNT(*), COUNT(equipment), COUNT(equipment_id) FROM eq_usage_daily").fetchone()
    n_drill = con.execute("SELECT COUNT(*) FROM eq_usage_daily WHERE equipment = 'DRILL' AND equipment_id IS NULL").fetchone()[0]
    con.close()
    assert after[:2] == before[:2] and after[2] == before[2] - 2 and n_drill == 2, (before, after, n_drill)


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


# ── 규칙의 가장자리 (단위 시험 — 파이프라인 없이) ─────────────────────────────
def _rec(pid, day, start, end, kind="meter", ref="id:x"):
    return {"page_id": pid, "work_date": day, "meter_start": start, "meter_end": end, "reading_kind": kind, "ref": ref,
            "start_field_id": f"{pid}:s", "end_field_id": f"{pid}:e"}


def test_continuity_never_invents_a_gap():
    from minedocscan.validate.usage import continuity_rows

    res = lambda recs: {r["page_id"]: (r["result"], r["diff"], r["days_between"]) for r in continuity_rows(recs)}   # noqa: E731
    base = [_rec("a", "2030-01-01", 100.0, 105.0), _rec("b", "2030-01-02", 105.0, 110.0), _rec("c", "2030-01-04", 112.0, 115.0)]
    assert res(base) == {"a": ("first", None, None), "b": ("match", 0.0, 0), "c": ("gap", 2.0, 1)}
    # 시작만 적힌 기록 뒤: 더 앞의 종료와 비교하지 않는다 (있는 날을 빠진 날로 탓하지 않게)
    start_only = [base[0], _rec("b", "2030-01-02", 105.0, None), base[2]]
    assert res(start_only)["c"][0] == "unknown"
    # 같은 날 시작 값을 아직 모르는 장이 있으면 (그 장이 앞일 수 있다) unknown — 검수가 끝나면 match
    night = _rec("n", "2030-01-02", 108.0, 110.0)
    day = _rec("d", "2030-01-02", None, None, kind="pending")
    assert res([base[0], night, day])["n"][0] == "unknown"
    assert res([base[0], _rec("d", "2030-01-02", 105.0, 108.0), night])["n"] == ("match", 0.0, 0)
    # 날짜를 모르는 쪽은 비교하지 않고, 다른 쪽의 앞 기록도 되지 않는다
    dateless = [_rec("z", None, 0.0, 200.0), _rec("b", "2030-01-02", 105.0, 110.0)]
    assert res(dateless) == {"z": ("unknown", None, None), "b": ("first", None, None)}


class _Site:
    def equipment_id_of(self, name):
        return None


def _usage_of(cells: dict, tmp_path):
    """합성 세로 양식(계기 표만 채운다)의 eq_usage_daily 행. cells: 칸 → (has_value, value_final, review_status)."""
    from minedocscan.handlers.usage import usage_rows

    _img, spec = synth_usage.build_usage_log()
    p = tmp_path / "t" / "template.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tpl = Template(p)
    frows = []
    for slot in ("start", "end", "total"):
        has, val, st = cells.get(slot, (0, None, "auto"))
        frows.append({"field_id": f"p:meter:{slot}:0", "region": "meter", "field_name": slot, "row_key": "reading", "row_no": 0,
                      "kind": "handwritten_number", "format": "reading", "has_value": has, "has_value_raw": has, "value_final": val,
                      "value_raw": None, "review_status": st, "confidence": None, "x0": 0})
    return usage_rows(_Site(), tpl, {"page_id": "p", "work_date": "2030-01-01", "template_name": tpl.name}, {}, frows)[0]


def test_hours_rules_at_the_edges(tmp_path):
    rv = lambda v: (1, v, "reviewed")                                                     # noqa: E731
    u = _usage_of({"start": rv("08:00"), "end": rv("17:00"), "total": rv("9")}, tmp_path)
    assert (u["reading_kind"], u["hours"], u["hours_basis"]) == ("clock", 9.0, "total")      # 총(길이)은 수 — 섞인 것이 아니다
    u = _usage_of({"start": rv("1000.0"), "end": rv("1009.0"), "total": rv("08:00")}, tmp_path)
    assert (u["reading_kind"], u["hours"], u["hours_basis"]) == ("meter", 9.0, "meter")
    u = _usage_of({"start": rv("1000.0"), "end": rv("17:00")}, tmp_path)
    assert (u["reading_kind"], u["hours"], u["hours_basis"]) == ("mixed", None, None)
    u = _usage_of({"start": rv("22:00"), "end": rv("06:00")}, tmp_path)
    assert (u["hours"], u["hours_basis"]) == (8.0, "clock")                                  # 자정을 넘는다
    # "읽을 수 없음": 기계가 빈 칸이라 했어도 모르는 칸 — 총으로 내려가지 않는다
    u = _usage_of({"start": (0, "", "pending"), "end": rv("1009.0"), "total": rv("9")}, tmp_path)
    assert (u["reading_kind"], u["hours"], u["hours_basis"]) == ("pending", None, None)


def test_illegible_tally_cell_makes_the_subtotal_unknown():
    from minedocscan.validate.usage import _int

    assert _int({"has_value": 0, "value_final": "", "review_status": "pending"}) is None
    assert _int({"has_value": 0, "value_final": "", "review_status": "reviewed"}) == 0
    assert _int({"has_value": 1, "value_final": "7", "review_status": "auto"}) == 7


def test_readings_never_carry_machine_or_earlier_values(usage_run, usage_synth, tmp_path):
    """readings 의 응답에는 기계 값도 앞날의 값도 없다 — 기계가 계기를 읽었고(소수·시각을 읽는 모델을 흉내 낸다 — 미룸, tasks/0006 1절) 앞날의 계기를 검수한 뒤에도."""
    con, site = clone_db(usage_run["pipe"].con), usage_run["pipe"].site
    settings = replace(usage_run["settings"], reviews=tmp_path / "r.jsonl")
    con.execute("UPDATE doc_field SET value_raw = '9999.9' WHERE region = 'meter' AND has_value_raw = 1")
    day0 = min(t["date"] for t in usage_synth.truth["usage"])
    first = {t["source"] for t in usage_synth.truth["usage"] if t["date"] == day0}
    rows = [f for f in usage_fields(con) if f["source"] in first and f["region"] == "meter"]
    for f in rows:                                                       # 첫날의 계기 칸만 정답대로
        text = usage_run["answers"].get((f["source"], f["template_name"], f["region"], f["field_name"], f["row_key"]))
        save(con, site, settings, Review(f["field_id"], "value", text, "jp") if text else Review(f["field_id"], "empty",
                                                                                                reviewer="jp"))
    q = build_queue(con, "readings", site=site)
    blob = json.dumps(q, ensure_ascii=False)
    later = [t for t in usage_synth.truth["usage"] if t["date"] != day0 and t["reading_kind"] != "empty"]
    assert q["items"] and len(q["items"]) == len(later) and rows
    assert "9999.9" not in blob
    for t in usage_synth.truth["usage"]:
        if t["date"] == day0:
            for v in (t["meter_end"], t["clock_end"]):
                assert v is None or (f"{v:.1f}" if isinstance(v, float) else v) not in blob


def test_usage_pages_sit_in_the_daily_bundle_with_the_other_forms(tmp_path):
    """usage_logs=True: 기본 양식 셋의 쪽 뒤에 가동 일보가 붙고, 다섯 양식이 서로 헷갈리지 않는다 (분류만 — 파이프라인은 돌리지 않는다)."""
    from minedocscan.forms.classify import FormClassifier
    from minedocscan.imaging.io import load_pages
    from minedocscan.tools.synth import generate

    r = generate(tmp_path / "d", days=1, seed=2, usage_logs=True)
    clf = FormClassifier(list(SitePack(r.site).templates.values()))
    doc = next(iter(r.truth["documents"]))
    want = [p["template"] for p in r.truth["documents"][doc]]
    got = [clf.classify(g).template for _n, g in load_pages(r.scans / f"{doc}.pdf", 200)]
    assert got == want and {T_USAGE, T_LOADER, "synth_inspection", "synth_haul_log", "synth_haul_matrix"} <= set(want)
    assert r.truth["usage"] and r.truth["days"][0]["haul_log"]


def test_eval_meta_counts_the_equipment_key(reviewed):
    """eval --meta 는 키 이름을 고르지 않는다: 기계가 장비명을 읽었다면(여기서는 흉내) equipment 의 정확도·자동 적재 오류를 낸다."""
    from minedocscan.evaluate.meta import evaluate_meta

    con = clone_db(reviewed["con"])
    con.execute("UPDATE doc_page_meta SET machine_value = value, machine_confidence = 0.99, machine_status = 'auto' "
                "WHERE meta_key = 'equipment'")
    pid = con.execute("SELECT page_id FROM doc_page_meta WHERE meta_key = 'equipment' ORDER BY page_id").fetchone()[0]
    con.execute("UPDATE doc_page_meta SET machine_value = 'DOZER' WHERE meta_key = 'equipment' AND page_id = ?", (pid,))
    # 평소의 장비가 아닌 장비를 쓴 쪽 하나 (LOADER 를 늘 쓰는 사람이 하루 SHOVEL 을 탔다 — 사람 값과 기계 값 모두)
    other = con.execute("SELECT m.page_id FROM doc_page_meta m WHERE m.meta_key = 'equipment' AND m.value = 'LOADER' "
                        "AND m.page_id <> ? ORDER BY m.page_id", (pid,)).fetchone()[0]
    con.execute("UPDATE doc_page_meta SET value = 'SHOVEL', machine_value = 'SHOVEL' WHERE meta_key = 'equipment' AND page_id = ?",
                (other,))
    k = evaluate_meta(con)["keys"]["equipment"]
    n = con.execute("SELECT COUNT(*) FROM doc_page_meta WHERE meta_key = 'equipment'").fetchone()[0]
    assert (k["n"], k["correct"], k["auto_error"]["wrong"]) == (n, n - 1, 1)
    assert (k["changed"]["n"], k["changed"]["correct"]) == (1, 1)
