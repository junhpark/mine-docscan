"""검수 기록: 추가 전용 파일 ↔ doc_review ↔ doc_field. 이미지 없이 도는 부분과 합성 하루치로 도는 부분."""
import json
import shutil
import sqlite3

import pytest

from minedocscan.review.store import (
    Review,
    append,
    apply_verdict,
    effective,
    import_into,
    load,
    make_review_id,
)
from minedocscan.store.db import SCHEMA_VERSION, SchemaVersionError, open_db, upsert

FID = "ab12cd34ef56ab12-p2:haul:trips_day:3"


def _rev(verdict="value", value="7", at="2030-01-08T01:02:03Z", reviewer="jp", **kw):
    return Review(field_id=FID, verdict=verdict, value=value, reviewer=reviewer, reviewed_at=at, **kw)


def test_review_id_is_deterministic_and_value_rules():
    a, b = _rev(), _rev()
    assert a.review_id == b.review_id == make_review_id(FID, "2030-01-08T01:02:03Z", "jp")
    assert _rev(reviewer="kim").review_id != a.review_id
    assert _rev(verdict="empty", value="무시됨").value == ""            # value 는 verdict=value 에만 있다
    with pytest.raises(ValueError):
        _rev(verdict="value", value="")
    with pytest.raises(ValueError):
        _rev(verdict="maybe")
    assert a.page_id == "ab12cd34ef56ab12-p2"


def test_file_roundtrip_with_korean_path_and_value(tmp_path):
    path = tmp_path / "현장 팩" / "reviews" / "reviews.jsonl"
    r = _rev(value="7회 (수정)", note="메모: 흐림", source="스캔_2030-01-07#2", bbox=[604, 694, 846, 766],
             machine={"has_value": 1, "value_raw": "", "backend": "null", "confidence": 0.0})
    assert append(path, r) == 1
    assert append(path, _rev(at="2030-01-08T01:02:04Z")) == 2
    raw = path.read_text(encoding="utf-8")
    assert "7회 (수정)" in raw and "\\u" not in raw                       # ensure_ascii=False
    reviews, skipped = load(path)
    assert skipped == 0 and [seq for seq, _ in reviews] == [1, 2]
    got = reviews[0][1]
    assert got == r and got.bbox == [604, 694, 846, 766] and got.machine["backend"] == "null"
    assert json.loads(raw.splitlines()[0])["review_id"] == r.review_id


def test_broken_last_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "reviews.jsonl"
    append(path, _rev())
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"review_id": "x", "field_id": "' + FID + '", "verd')        # 쓰다 끊긴 줄 (줄바꿈 없음)
    reviews, skipped = load(path)
    assert len(reviews) == 1 and skipped == 1
    # 그 뒤에 추가해도 새 줄은 온전하다: 끊긴 줄은 그대로 한 줄로 남고 새 줄은 다음 줄
    assert append(path, _rev(at="2030-01-08T02:00:00Z")) == 3
    reviews, skipped = load(path)
    assert [seq for seq, _ in reviews] == [1, 3] and skipped == 1


def test_import_is_idempotent_and_effective_is_latest(tmp_path):
    path = tmp_path / "reviews.jsonl"
    append(path, _rev(value="1", at="2030-01-08T01:00:00Z"))
    append(path, _rev(value="3", at="2030-01-08T03:00:00Z"))
    append(path, _rev(value="2", at="2030-01-08T02:00:00Z", reviewer="kim"))     # 늦게 써졌지만 시각은 앞선다
    other = Review(field_id="ab12cd34ef56ab12-p3:haul:trips_day:0", verdict="empty", reviewer="jp",
                   reviewed_at="2030-01-08T01:00:00Z")
    append(path, other)
    con = open_db("sqlite:///:memory:")
    assert import_into(con, path)["imported"] == 4
    assert import_into(con, path)["imported"] == 4
    assert con.execute("SELECT COUNT(*) FROM doc_review").fetchone()[0] == 4
    eff = effective(con)
    assert eff[FID].value == "3" and eff[other.field_id].verdict == "empty"
    assert set(effective(con, page_id="ab12cd34ef56ab12-p2")) == {FID}
    assert set(effective(con, field_ids=[other.field_id])) == {other.field_id}
    assert effective(con, field_ids=[]) == {}
    # 같은 시각이면 파일에서 뒤의 줄이 이긴다
    append(path, _rev(value="9", at="2030-01-08T03:00:00Z", reviewer="kim"))
    import_into(con, path)
    assert effective(con)[FID].value == "9"
    assert import_into(con, tmp_path / "없는파일.jsonl") == {"path": str(tmp_path / "없는파일.jsonl"), "imported": 0,
                                                            "skipped": 0}


def test_apply_verdict_keeps_machine_values():
    row = {"field_id": FID, "has_value_raw": 1, "has_value": 1, "value_raw": "7", "value_final": "7",
           "confidence": 0.5, "backend": "ocr-x", "review_status": "pending", "reviewed_by": None, "reviewed_at": None}
    v = apply_verdict(row, _rev(value="1"))
    assert (v["value_final"], v["has_value"], v["review_status"], v["reviewed_by"]) == ("1", 1, "reviewed", "jp")
    assert (v["value_raw"], v["confidence"], v["backend"], v["has_value_raw"]) == ("7", 0.5, "ocr-x", 1)
    e = apply_verdict(row, _rev(verdict="empty"))
    assert (e["value_final"], e["has_value"], e["review_status"]) == ("", 0, "reviewed")
    i = apply_verdict(row, _rev(verdict="illegible"))
    assert (i["value_final"], i["has_value"], i["review_status"]) == ("7", 1, "pending")
    assert row["review_status"] == "pending"                                      # 원본은 건드리지 않는다


def test_old_schema_db_is_refused(tmp_path):
    # 버전 기록이 없던 예전 DB
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE doc_document (document_id TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(SchemaVersionError, match="--fresh"):
        open_db(f"sqlite:///{old.as_posix()}")
    # 버전이 다른 DB
    con = open_db(f"sqlite:///{(tmp_path / 'v.db').as_posix()}")
    upsert(con, "meta_schema", {"key": "schema_version", "value": str(SCHEMA_VERSION + 1)})
    con.commit()
    con.close()
    with pytest.raises(SchemaVersionError):
        open_db(f"sqlite:///{(tmp_path / 'v.db').as_posix()}")
    # 같은 버전은 다시 열린다 (멱등)
    path = f"sqlite:///{(tmp_path / 'ok.db').as_posix()}"
    open_db(path).close()
    con = open_db(path)
    assert con.execute("SELECT value FROM meta_schema WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)


# ── 합성 하루치로: 재실행에 붙이기, 업무 테이블 전파, 불변식 ───────────────────────
from dataclasses import replace  # noqa: E402

from conftest import run_day  # noqa: E402
from minedocscan.pipeline import Pipeline  # noqa: E402
from minedocscan.recognize import OracleRecognizer, load_answers_json  # noqa: E402
from minedocscan.report import build_report  # noqa: E402
from minedocscan.review.store import save  # noqa: E402
from minedocscan.tools.synth import T_INSP, expected_xcheck  # noqa: E402

TABLES = ("doc_field", "prod_haul", "insp_daily", "xcheck_haul", "eq_assignment_obs", "doc_document", "doc_page_meta",
          "eq_usage_daily", "prod_tally", "xcheck_usage")


SKIP_COLUMNS = ("created_at", "received_at", "work_requested", "work_done")   # 실행·등록 시각과 요청 번호 (tasks/0007 4.8)


def _dump(con, table):
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})") if r[1] not in SKIP_COLUMNS]
    return sorted(tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {table}"))


def _haul_fields(con):
    """운반 숫자 셀 전부: (field_id, 정답 키)."""
    return con.execute(
        "SELECT f.field_id, d.source_name || '#' || p.page_no AS source, p.template_name, f.region, f.field_name, f.row_key "
        "FROM prod_haul h JOIN doc_field f ON h.source_field_id = f.field_id JOIN doc_page p ON f.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id ORDER BY f.field_id").fetchall()


@pytest.fixture(scope="module")
def day1(tmp_path_factory):
    """하루치 합성 데이터를 null 백엔드로 돌리고, 운반 셀 전부와 점검내역 몇 개를 검수로 저장해 둔다."""
    synth, settings, pipe = run_day(tmp_path_factory.mktemp("review_day1"))
    before = build_report(pipe.con)
    answers = load_answers_json(synth.answers_path)
    con, site = pipe.con, pipe.site

    # 1) 운반 셀 전부를 정답대로 (값이 있으면 value, 없으면 empty)
    for f in _haul_fields(con):
        text = answers.get((f["source"], f["template_name"], f["region"], f["field_name"], f["row_key"]))
        v = Review(f["field_id"], "value", text, "jp") if text else Review(f["field_id"], "empty", reviewer="jp")
        save(con, site, settings, v)
    # 2) 점검내역: 값 하나, 빈 칸 하나, 읽을 수 없음 하나, 그리고 한 셀은 두 번(나중 것이 유효)
    insp = {r["row_key"]: r["field_id"] for r in con.execute(
        "SELECT f.row_key, f.field_id FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
        "WHERE p.template_name = ? AND f.field_name = 'remark' ORDER BY f.row_no", (T_INSP,))}
    keys = list(insp)
    save(con, site, settings, Review(insp[keys[0]], "value", "oil leak (검수)", "jp", reviewed_at="2030-01-08T01:00:00Z"))
    save(con, site, settings, Review(insp[keys[0]], "value", "oil leak", "jp", reviewed_at="2030-01-08T01:00:01Z"))
    save(con, site, settings, Review(insp[keys[1]], "empty", reviewer="jp"))
    save(con, site, settings, Review(insp[keys[2]], "illegible", reviewer="jp"))
    con.commit()
    return {"synth": synth, "settings": settings, "pipe": pipe, "before": before, "answers": answers,
            "insp": {"value": insp[keys[0]], "empty": insp[keys[1]], "illegible": insp[keys[2]]}}


def test_full_day_review_makes_crosscheck_compare_trips(day1):
    con, synth = day1["pipe"].con, day1["synth"]
    day = synth.truth["days"][0]
    assert day1["before"]["xcheck_haul"] == expected_xcheck([day], with_trips=False)     # 검수 전: 값 유무만
    assert build_report(con)["xcheck_haul"] == expected_xcheck([day], with_trips=True)   # 검수 후: 횟수까지
    # 횟수가 다른 칸은 기계 값 기준(null → NULL)이 아니라 최종 값으로 잡혔다
    want = {(x["slot"], x["material"], x["level"]) for x in day["discrepancies"]}
    got = {tuple(r) for r in con.execute("SELECT slot, material, level FROM xcheck_haul WHERE status='mismatch'")}
    assert got == want
    assert con.execute("SELECT COUNT(*) FROM xcheck_haul WHERE log_trips_raw IS NOT NULL").fetchone()[0] == 0
    # 운반 행은 전부 reviewed, trips 는 최종값, trips_raw 는 기계값(null 이라 NULL)
    assert {r[0] for r in con.execute("SELECT DISTINCT review_status FROM prod_haul")} == {"reviewed"}
    assert con.execute("SELECT COUNT(*) FROM prod_haul WHERE trips_raw IS NOT NULL").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM prod_haul WHERE has_value=1 AND trips IS NULL").fetchone()[0] == 0


def test_verdicts_on_fields_and_inspection_rows(day1):
    con, ids = day1["pipe"].con, day1["insp"]
    f = lambda fid: con.execute("SELECT * FROM doc_field WHERE field_id=?", (fid,)).fetchone()   # noqa: E731
    v, e, i = f(ids["value"]), f(ids["empty"]), f(ids["illegible"])
    assert (v["value_final"], v["has_value"], v["review_status"], v["reviewed_by"]) == ("oil leak", 1, "reviewed", "jp")
    assert (e["value_final"], e["has_value"], e["review_status"]) == ("", 0, "reviewed")
    assert i["review_status"] == "pending" and i["reviewed_by"] == "jp"
    for r in (v, e, i):                                       # 기계 값은 그대로
        assert r["value_raw"] == ("" if r["has_value_raw"] else "") and r["backend"] in ("null", "ink")
    rows = {r["source_field_id"]: r for r in con.execute("SELECT * FROM insp_daily")}
    assert rows[ids["value"]]["remark"] == "oil leak" and rows[ids["value"]]["review_status"] == "reviewed"
    assert rows[ids["empty"]]["remark"] == "" and rows[ids["empty"]]["review_status"] in ("reviewed", "pending")
    assert rows[ids["illegible"]]["review_status"] == "pending"
    # 기존 항목은 검수가 없을 때와 같다 (점검 유/무 판정은 검수와 무관)
    rep = build_report(con)
    for k in ("documents", "pages", "pages_by_form", "align", "equipment"):
        assert rep[k] == day1["before"][k]
    assert rep["fields"]["pending"] < day1["before"]["fields"]["pending"]


def test_rebuild_from_review_file_equals_live_db(day1, tmp_path):
    """4.4 의 불변식: save 를 거친 DB == 같은 검수 파일로 새 WORK_ROOT 에서 처음부터 돌린 DB."""
    live = day1["pipe"].con
    fresh = Pipeline(replace(day1["settings"], work_root=tmp_path / "work2"))
    fresh.run([day1["synth"].scans])
    assert fresh.summary["reviews"]["imported"] == live.execute("SELECT COUNT(*) FROM doc_review").fetchone()[0]
    assert build_report(fresh.con) == build_report(live)
    for t in TABLES:
        assert _dump(fresh.con, t) == _dump(live, t), t


def test_oracle_rerun_keeps_machine_raw_and_review_final(day1, tmp_path):
    """검수된 셀도 인식기를 돌린다: value_raw 는 오라클 값, value_final 은 검수값."""
    con, answers = day1["pipe"].con, day1["answers"]
    f = next(r for r in _haul_fields(con)
             if (r["source"], r["template_name"], r["region"], r["field_name"], r["row_key"]) in answers)
    truth = answers[(f["source"], f["template_name"], f["region"], f["field_name"], f["row_key"])]
    wrong = str(int(truth) + 1)
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_bytes(day1["settings"].reviews.read_bytes())
    append(reviews, Review(f["field_id"], "value", wrong, "kim"))             # 일부러 정답과 다르게
    s = replace(day1["settings"], work_root=tmp_path / "work3", reviews=reviews)
    pipe = Pipeline(s, recognizer=OracleRecognizer(answers))
    pipe.run([day1["synth"].scans])
    r = pipe.con.execute("SELECT * FROM doc_field WHERE field_id=?", (f["field_id"],)).fetchone()
    assert (r["value_raw"], r["backend"], r["value_final"], r["review_status"]) == (truth, "oracle", wrong, "reviewed")
    h = pipe.con.execute("SELECT * FROM prod_haul WHERE haul_id=?", (f["field_id"],)).fetchone()
    assert (h["trips_raw"], h["trips"], h["review_status"]) == (int(truth), int(wrong), "reviewed")
    # 교차검증: 기계 값 기준 횟수는 양쪽 다 적혀 있다
    assert pipe.con.execute("SELECT COUNT(*) FROM xcheck_haul WHERE log_trips_raw IS NOT NULL "
                            "AND matrix_trips_raw IS NOT NULL").fetchone()[0] > 0


def test_save_for_unknown_field_only_records(day1):
    """DB 에 없는 필드(다른 WORK_ROOT 의 것)도 파일과 doc_review 에는 남는다 — 나중에 그 페이지가 적재되면 붙는다."""
    con, settings, site = day1["pipe"].con, day1["settings"], day1["pipe"].site
    n = con.execute("SELECT COUNT(*) FROM doc_review").fetchone()[0]
    out = save(con, site, settings, Review("0000000000000000-p1:haul:trips_day:0", "value", "3", "jp"))
    assert out["applied"] is False
    assert con.execute("SELECT COUNT(*) FROM doc_review").fetchone()[0] == n + 1


# ── 다시 처리의 불변식 (tasks/0007 4.8) ────────────────────────────────────
@pytest.mark.slow
def test_reprocess_invariant_with_decisions_and_reviews(world):
    """종류마다 한 번씩 (여섯 번). 더 긴 흔들기는 아래 (둘 다 -m slow — CI 의 slow 작업, 기본 시험 시간을 1.25배 안에)."""
    reprocess_fuzz(world, steps=6, seed=70072)


@pytest.mark.slow
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_reprocess_invariant_long(world, seed):
    reprocess_fuzz(world, steps=40, seed=seed, damaged=True)


def reprocess_fuzz(world, steps: int, seed: int, fresh: bool = True, on_touched=None, after_step=None,
                   damaged: bool = False) -> None:
    """결정(날짜 바꾸기 — 다른 문서가 있는 날짜로도, 버리기, 되살리기 — 문서·쪽)과 검수(칸·차량번호·작성자)를 섞어 steps 번 넣고
    그때마다 대기 중인 문서를 처리한(watch 한 바퀴) DB 가 같은 파일·검수·결정으로 처음부터 만든 DB 와 같다. 묶음(conftest.BUNDLES)은
    2030-01-07 에 일보 문서 셋·점검표 두 쪽·T01 일보 두 쪽, 가동 일보 사흘. 없는 칸(버린 쪽)의 검수도 넣는다 — 되살리면 붙는다.
    fresh=False 면 처음부터 만든 DB 와 견주지 않는다. on_touched: 검수가 건드린 것을 받는 곳 (주면 save(touched=…)),
    after_step(step): 걸음마다 처리한 뒤에 부른다 — 통합 DB 싣기를 끼운 판 (test_publish_pg.py).
    damaged=True: 끝이 잘린 날짜 없는 이름의 PDF 하나를 더 넣고(failed) 문서 단위 결정의 절반쯤을 그 문서에 건다 — 버린 실패 문서
    (tasks/0010 4.2). 쪽 수가 없는 문서라 쪽을 고르는 곳·쪽 결정에서는 뺀다. 주지 않으면 난수의 쓰임이 전과 같다."""
    from minedocscan.touched import Touched

    def save_(review):
        if on_touched is None:
            return save(con, site, st, review)
        t = Touched()
        out = save(con, site, st, review, touched=t)
        on_touched(t)
        return out

    import random

    from minedocscan.intake import decisions as decs
    from minedocscan.review.store import field_format, field_id_of
    from minedocscan.tools.synth import SLOTS, T_LOG
    from test_reprocess import BUSINESS, RECEIVED, assert_same, dump, fresh_of, no_null_dates

    pipe, st, site = world["pipe"], world["st"], world["site"]
    con = pipe.con
    docs = {r["document_id"]: r["n_pages"] for r in con.execute("SELECT document_id, n_pages FROM doc_document")}
    fields = {r["field_id"]: dict(r) for r in con.execute(
        "SELECT f.field_id, f.kind, f.region, f.field_name, p.template_name FROM doc_field f JOIN doc_page p "
        "ON f.page_id = p.page_id WHERE f.kind LIKE 'handwritten%' AND f.region <> 'fields' ORDER BY f.field_id")}
    logs = [r[0] for r in con.execute("SELECT page_id FROM doc_page WHERE template_name = ? ORDER BY page_id", (T_LOG,))]
    days = ["2030-01-07", "2030-01-08", "2030-01-09", "2030-01-10"]
    samples = {None: ["3", "oil leak"], "integer": ["2", "11"], "decimal": ["1.5"], "time": ["08:00"],
               "time_range": ["08:00~17:00"], "reading": ["1234.5", "1240.0", "08:00"]}
    rng = random.Random(seed)
    discarded: set[str] = set()
    done = []
    bad = None
    if damaged:
        data = (world["scans"] / "b_2030-01-07.pdf").read_bytes()
        (world["scans"] / "손상 묶음.pdf").write_bytes(data[: len(data) // 2])
        pipe.run([world["scans"] / "손상 묶음.pdf"])
        bad = con.execute("SELECT document_id FROM doc_document WHERE source_name = '손상 묶음'").fetchone()[0]
        assert con.execute("SELECT status FROM doc_document WHERE document_id = ?", (bad,)).fetchone()[0] == "failed"
    kinds = {"review": 4, "meta": 1, "doc_date": 2, "page_date": 2, "doc_discard": 2, "page_discard": 2}
    weighted = [k for k, n in kinds.items() for _ in range(n)]
    plan = (rng.sample(list(kinds), len(kinds)) + rng.choices(weighted, k=max(0, steps - len(kinds))))[:steps]
    for step, kind in enumerate(plan):
        now = f"2030-01-10T{step // 60:02d}:{step % 60:02d}:00Z"
        doc = rng.choice(sorted(docs))
        page = f"{doc}-p{rng.randint(1, docs[doc])}"
        if kind == "review":                                     # 칸 하나 — 지금 DB 에 없는 칸(버린 쪽)일 수도 있다
            f = fields[rng.choice(sorted(fields))]
            verdict = rng.choice(["value", "value", "empty", "illegible"])
            fmt = field_format(site, f["template_name"], f["region"], f["field_name"])
            value = rng.choice(samples.get(fmt, samples[None])) if verdict == "value" else ""
            save_(Review(f["field_id"], verdict, value, "jp", reviewed_at=now))
            done.append((kind, f["field_id"], verdict, value))
        elif kind == "meta":
            pg = rng.choice(logs)
            _slot, vehicle, operator = rng.choice(SLOTS)
            name, value = rng.choice([("vehicle_no", vehicle), ("operator", operator)])
            save_(Review(field_id_of(pg, name), "value", value, "jp", reviewed_at=now))
            done.append((kind, pg, name, value))
        else:
            if bad is not None and kind.startswith("doc") and rng.random() < 0.5:
                doc = bad                                        # 손상 문서 — 문서 단위 결정만
            target = doc if kind.startswith("doc") else page
            if kind.endswith("date"):
                item = {"target": target, "kind": "date", "value": rng.choice(days)}
            else:
                item = {"target": target, "kind": "restore" if target in discarded else "discard"}
                discarded ^= {target}
            decs.save(con, st.decisions_path(site.root), [item], "jp", received=RECEIVED, now=now)
            done.append((kind, item["target"], item["kind"], item.get("value", "")))
        pipe.process_pending()
        assert pipe.pending_documents() == []
        no_null_dates(con)
        if after_step is not None:
            after_step(step)
        if not fresh:
            continue
        other = fresh_of(st, site, world["scans"], world["root"], f"fresh{step}")
        assert_same(dump(con), dump(other.con), (seed, step, done[-3:]))
        other.con.close()
        shutil.rmtree(world["root"] / f"fresh{step}")
    assert {k for k, *_ in done} == set(kinds) or steps < len(kinds)
    assert bad is None or any(d[1] == bad for d in done)
    assert con.execute("SELECT COUNT(*) FROM xcheck_haul").fetchone()[0] > 0
    assert sum(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in BUSINESS) > 0
