"""소수·시각 모델로 합성 가동 일보를 끝에서 끝까지 (tasks/0011 단계 3) — torch 없이, 시험용 모델 tests/fixtures/readings-fixture.

[recognize.by_format] 넷 = "readings" + [recognize.readings] model = 시험용 모델로 `run` (명령으로 — 설정·인식기 만들기를 지난다).
합성 가동 일보(--usage-logs)는 인쇄 층 없이 — 근무 시각 칸의 인쇄된 "~" 가 잉크로 보여 인식기에 간다 (빈 칸으로 읽어야 한다).
  · 값이 있는 형식 칸의 기계 값(raw)이 정답과 같은 값(formats.same_value)
  · 자동 적재된 형식 칸 중 틀린 것, 자동 적재의 몫 (분자·분모·윌슨 상한)
  · 점으로 쓴 시각으로 읽힌 칸은 늘 검수 대기, 총이 어긋난 쪽(TRUCK_total_mismatch)의 계기 칸은 자동 적재 0
  · 인쇄된 ~ 만 있는 근무 시각 칸: 빈 칸으로 자동 적재, 값으로 자동 적재된 것 0
  · 자동 적재된 쪽의 eq_usage_daily = 정답, 쪽 안 검산으로 내린 칸의 수(이유별)
자동 적재 기준은 설정의 [recognize.readings] auto_accept_conf = 0.5 (4.3 — 설정 > 카드). 카드의 기준(0.8 — 합성 칸 검증에서 오류 1 %
이하로 고른 값)은 합성 가동 일보의 값에 보수적이라 몫이 0.2 안팎이다 — 지시서의 문턱(몫 0.5)을 이렇게 고쳤다 (tasks/0011 12절).
틀린 값은 쪽 안 검산이 막는다. slow 는 카드의 기준으로 돈 수치도 보고한다.
기본 시험은 3일치, slow 는 15일치 (분모가 100 을 넘게) + 형식 칸을 읽기 전과의 처리 시간.
수치는 합성 칸에 대한 문턱이다 — 손글씨 인식률이 아니다.
"""
from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from minedocscan.cli import main
from minedocscan.evaluate.stats import wilson
from minedocscan.forms.formats import dotted_clock, same_value
from minedocscan.recognize import READ_FORMATS, load_answers_json
from minedocscan.store.db import open_db
from minedocscan.tools.synth import generate
from minedocscan.tools.synth_usage import T_LOADER

FIXTURE = Path(__file__).parent / "fixtures" / "readings-fixture"
AUTO_CONF = 0.5                   # 시험의 자동 적재 기준 (위의 설명). None = 카드의 기준
USAGE_KEYS = ("reading_kind", "meter_start", "meter_end", "meter_total", "clock_start", "clock_end", "shift_minutes", "hours_basis")


def config(root: Path, read: bool = True, conf: float | None = AUTO_CONF) -> str:
    lines = ["[pipeline]", "save_aligned = false", "[paths]", f"reviews = {json.dumps(str(root / 'reviews.jsonl'))}"]
    if read:
        lines += ["[recognize.by_format]", *[f'{f} = "readings"' for f in READ_FORMATS],
                  "[recognize.readings]", f"model = {json.dumps(str(FIXTURE))}"]
        if conf is not None:
            lines.append(f"auto_accept_conf = {conf}")
    cfg = root / f"minedocscan-{int(read)}-{conf}.toml"
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(cfg)


def run(synth, root: Path, read: bool = True, conf: float | None = AUTO_CONF) -> dict:
    work = root / f"work-{int(read)}-{conf}"
    args = ["run", "--json", "--config", config(root, read, conf), "--site", str(synth.site), "--archive-root", str(synth.scans),
            "--work-root", str(work)]
    buf = io.StringIO()
    t0 = time.perf_counter()
    with redirect_stdout(buf):
        assert main(args) == 0
    seconds = time.perf_counter() - t0
    return {"con": open_db(f"sqlite:///{work / 'minedocscan.db'}"), "out": json.loads(buf.getvalue()), "seconds": seconds,
            "synth": synth}


def format_cells(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT f.*, d.source_name || '#' || p.page_no AS source, p.template_name FROM doc_field f "
        "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
        "WHERE f.format IN ('decimal', 'time', 'time_range', 'reading') AND f.region != 'fields' AND f.kind LIKE 'handwritten%'")]


def measure(r: dict) -> dict:
    """그 실행의 형식 칸 수치 (값 없이 수만)."""
    con, synth = r["con"], r["synth"]
    answers = {(src, region, name, row): text for (src, _tpl, region, name, row), text in load_answers_json(synth.answers_path).items()
               if region in ("meter", "shifts")}
    truth = {t["source"]: t for t in synth.truth["usage"]}
    m = {"cells": 0, "values": 0, "raw_correct": 0, "sent": 0, "auto": 0, "auto_wrong": 0, "auto_value": 0, "auto_empty": 0,
         "dotted": 0, "dotted_not_pending": 0, "mismatch_auto": 0, "tilde_only": 0, "tilde_auto_empty": 0, "tilde_auto_value": 0,
         "by_format": {}}
    for c in format_cells(con):
        key = (c["source"], c["region"], c["field_name"], c["row_key"])
        want = answers.get(key, "")
        m["cells"] += 1
        f = m["by_format"].setdefault(c["format"], {"values": 0, "raw_correct": 0, "auto": 0, "auto_wrong": 0})
        sent = c["backend"] == "readings"
        m["sent"] += sent
        if want:
            m["values"] += 1
            f["values"] += 1
            ok = sent and bool(c["value_raw"]) and same_value(c["format"], c["value_raw"], want)
            m["raw_correct"] += ok
            f["raw_correct"] += ok
        if sent and c["format"] == "reading" and c["value_raw"] and dotted_clock(c["value_raw"]) is not None:
            m["dotted"] += 1
            m["dotted_not_pending"] += not (c["status_raw"] == "pending" and c["value_final"] is None)
        if c["status_raw"] == "auto" and sent:
            m["auto"] += 1
            f["auto"] += 1
            right = (c["value_final"] == want) if want else (not c["has_value"] and c["value_final"] in (None, ""))
            m["auto_wrong"] += not right
            f["auto_wrong"] += not right
            m["auto_value" if c["has_value"] else "auto_empty"] += 1
        t = truth.get(c["source"])
        if t and "TRUCK_total_mismatch" in t["scenarios"] and c["region"] == "meter" and c["status_raw"] == "auto" and want:
            m["mismatch_auto"] += 1
        if c["region"] == "shifts" and not want:                        # 인쇄된 ~ 만 있는 근무 시각 칸
            m["tilde_only"] += 1
            if c["status_raw"] == "auto" and sent:
                m["tilde_auto_empty" if not c["has_value"] else "tilde_auto_value"] += 1
    lo, hi = wilson(m["auto_wrong"], m["auto"])
    m["auto_error_ci95"] = [round(lo, 4), round(hi, 4)]
    m["raw_accuracy"] = round(m["raw_correct"] / m["values"], 4) if m["values"] else None
    m["auto_share"] = round(m["auto"] / m["sent"], 4) if m["sent"] else None
    m["gate"] = {k: v for k, v in r["out"]["run"]["handlers"].get("usage", {}).items() if k.startswith("format_gate_")}
    m["pages"] = con.execute("SELECT COUNT(*) FROM doc_page WHERE status = 'loaded'").fetchone()[0]
    return m


def usage_agrees(r: dict) -> tuple[int, int]:
    """자동 적재된 쪽의 가동 기록이 정답과 같은가: (자동 적재된 쪽, 그중 다른 쪽)."""
    con, truth = r["con"], {t["source"]: t for t in r["synth"].truth["usage"]}
    auto = wrong = 0
    for u in con.execute("SELECT u.*, d.source_name || '#' || p.page_no AS source FROM eq_usage_daily u JOIN doc_page p "
                         "ON u.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id"):
        if u["review_status"] != "auto":
            continue
        auto += 1
        t = truth[u["source"]]
        same = all(u[k] == t[k] for k in USAGE_KEYS) and (
            u["hours"] == pytest.approx(t["hours"]) if t["hours"] is not None else u["hours"] is None)
        wrong += not same
    return auto, wrong


def check(m: dict, agree: tuple[int, int]) -> None:
    print("형식 칸:", json.dumps(m, ensure_ascii=False), "· 자동 적재된 쪽의 가동 기록 (쪽, 다른 쪽):", agree)
    assert m["values"] and m["raw_accuracy"] >= 0.95
    assert m["auto_wrong"] <= (0.01 * m["auto"] if m["auto"] >= 100 else 1)
    assert m["auto_share"] >= 0.5
    assert m["dotted_not_pending"] == 0
    assert m["mismatch_auto"] == 0
    assert m["tilde_only"] and m["tilde_auto_value"] == 0 and m["tilde_auto_empty"] >= 0.9 * m["tilde_only"]
    assert agree[0] > 0 and agree[1] == 0


@pytest.fixture(scope="module")
def usage3(tmp_path_factory):
    root = tmp_path_factory.mktemp("readings_e2e")
    synth = generate(root / "data", days=3, seed=0, usage_only=True)
    return run(synth, root)


def test_readings_read_the_synthetic_usage_logs(usage3):
    check(measure(usage3), usage_agrees(usage3))


def test_pages_with_the_dotted_clock_and_the_mismatched_total_wait_for_people(usage3):
    con = usage3["con"]
    truth = usage3["synth"].truth["usage"]
    pump = [t["source"] for t in truth if "PUMP_clock_unaliased" in t["scenarios"]]
    rows = {r["source"]: r for r in (dict(x) for x in con.execute(
        "SELECT u.review_status, u.reading_kind, d.source_name || '#' || p.page_no AS source FROM eq_usage_daily u "
        "JOIN doc_page p ON u.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id"))}
    dotted_pages = [s for s in pump if any(c["source"] == s and c["format"] == "reading" and c["value_raw"]
                                           and dotted_clock(c["value_raw"]) for c in format_cells(con))]
    for s in dotted_pages:                                    # 점으로 쓴 시각이 하나라도 있으면 그 쪽은 사람이 정한다
        assert rows[s]["review_status"] == "pending"
    for t in truth:
        if "TRUCK_total_mismatch" in t["scenarios"]:
            assert rows[t["source"]]["review_status"] == "pending"
    assert any(t["template"] == T_LOADER for t in truth)


@pytest.mark.slow
def test_fifteen_days_and_the_time_it_costs(tmp_path):
    """15일치: 분모(자동 적재된 형식 칸)가 100 을 넘는다. 같은 묶음을 형식 칸을 읽지 않고(설정하지 않은 상태) 돌린 것과 쪽당 시간을 견준다."""
    synth = generate(tmp_path / "data", days=15, seed=3, usage_only=True)
    read = run(synth, tmp_path, read=True)
    m = measure(read)
    check(m, usage_agrees(read))
    assert m["auto"] >= 100
    card = measure(run(synth, tmp_path, conf=None))                     # 카드의 기준으로 (보고 — 문턱 없이, 안전한 것만 본다)
    print("카드의 기준:", json.dumps({k: card[k] for k in ("auto", "auto_value", "auto_empty", "auto_wrong", "auto_error_ci95",
                                                       "auto_share", "gate")}, ensure_ascii=False))
    assert card["auto_wrong"] <= max(1, 0.01 * card["auto"]) and card["dotted_not_pending"] == 0 and card["mismatch_auto"] == 0
    unread = run(synth, tmp_path, read=False)
    pages = m["pages"]
    print(f"쪽 {pages}개: 읽기 {read['seconds'] / pages:.2f} s/쪽, 읽지 않기 {unread['seconds'] / pages:.2f} s/쪽, "
          f"형식 칸 {m['sent']}개를 읽은 몫 {(read['seconds'] - unread['seconds']) / max(m['sent'], 1) * 1000:.0f} ms/칸")
