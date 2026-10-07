"""결정 기록과 사람이 넣는 날짜 (tasks/0007 4.2·4.3) — DB 와 파일만, 파이프라인 없이."""
from __future__ import annotations

import json
from datetime import UTC, date, timedelta, timezone

import pytest

from minedocscan.config import Settings
from minedocscan.intake import decisions as decs
from minedocscan.intake.dates import DateError, iso_date, parse_date, warning
from minedocscan.store.db import open_db

RECEIVED = date(2026, 3, 27)


@pytest.mark.parametrize("text, want", [
    ("2025-03-26", "2025-03-26"), ("2025.3.26", "2025-03-26"), ("20250326", "2025-03-26"),
    ("25.03.26", "2025-03-26"), ("25/03/26", "2025-03-26"), ("250326", "2025-03-26"),
    ("03-26", "2026-03-26"), ("3.26", "2026-03-26"), ("0326", "2026-03-26"),
    ("0328", "2025-03-28"),                    # 받은 날(03-27)을 넘지 않는 가장 가까운 해
    ("0229", "2024-02-29"),                    # 윤년까지 거슬러 간다
    (" 2026-03-27 ", "2026-03-27"),
])
def test_parse_date_forms(text, want):
    assert parse_date(text, RECEIVED) == want


@pytest.mark.parametrize("text", ["2025-02-30", "250230", "0230", "1301", "2025-13-01", "", "어제", "2025-03", "123"])
def test_parse_date_rejects(text):
    with pytest.raises(DateError):
        parse_date(text, RECEIVED)


def test_warning_and_iso_date():
    assert warning("2026-03-27", RECEIVED) is None and warning("2026-02-25", RECEIVED) is None
    assert "뒤" in warning("2026-03-28", RECEIVED)
    assert "31일" in warning((RECEIVED - timedelta(days=32)).isoformat(), RECEIVED)
    assert iso_date("2030-01-07") == "2030-01-07"
    assert iso_date("30.01.07") is None and iso_date("2030-02-30") is None and iso_date(None) is None
    assert iso_date("2030-1-07") is None


def test_received_day_is_the_local_calendar_day():
    """받은 시각은 UTC — 받은 날은 그 컴퓨터의 날짜다 (한국 시각 03-27 08:30 = UTC 03-26 23:30)."""
    row = {"received_at": "2026-03-26T23:30:00.000Z"}
    kst = timezone(timedelta(hours=9))
    assert decs.received_day(row, kst) == date(2026, 3, 27)
    assert decs.received_day(row, UTC) == date(2026, 3, 26)
    assert parse_date("0327", decs.received_day(row, kst)) == "2026-03-27"


def test_targets_are_normalized_and_validated():
    assert decs.normalize_target(" ABCDEF0123456789-p01 ") == "abcdef0123456789-p1"
    assert decs.split_target("abcdef0123456789-p12") == ("abcdef0123456789", 12)
    assert decs.split_target("abcdef0123456789") == ("abcdef0123456789", None)
    with pytest.raises(decs.DecisionError, match="쪽에만"):
        decs.Decision("abcdef0123456789", "keep", decided_by="jp")
    with pytest.raises(decs.DecisionError, match="ISO"):
        decs.Decision("abcdef0123456789", "date", "0326", decided_by="jp")
    with pytest.raises(decs.DecisionError, match="알 수 없는"):
        decs.Decision("abcdef0123456789", "merge", decided_by="jp")
    with pytest.raises(decs.DecisionError):
        decs.Decision("abcdef0123456789", "discard")             # 결정한 사람이 없다


@pytest.fixture
def db(tmp_path):
    con = open_db(f"sqlite:///{tmp_path / 'w' / 'minedocscan.db'}")
    for doc, n in (("aaaaaaaaaaaaaaaa", 3), ("bbbbbbbbbbbbbbbb", 1)):
        con.execute("INSERT INTO doc_document (document_id, source_path, source_name, n_pages, status, created_at, "
                    "received_at) VALUES (?, ?, ?, ?, 'processed', '2026-03-27T00:00:00', '2026-03-27T00:00:00.000Z')",
                    (doc, f"/x/{doc}.pdf", doc, n))
    con.commit()
    return con, Settings(reviews=tmp_path / "기록" / "reviews.jsonl").decisions_path(None)


def requested(con, doc):
    return con.execute("SELECT work_requested FROM doc_document WHERE document_id = ?", (doc,)).fetchone()[0]


def test_save_appends_and_effective_is_the_last_per_group(db):
    con, path = db
    a = "aaaaaaaaaaaaaaaa"
    out = decs.save(con, path, [{"target": a, "kind": "date", "value": "0326"},
                                {"target": f"{a}-p02", "kind": "discard", "note": "메모"}], "jp", received=RECEIVED)
    assert out["warnings"] == [] and out["documents"] == [a] and requested(con, a) == 1
    assert [d.target for d in out["decisions"]] == [a, f"{a}-p2"]                   # 쪽 ID 의 표기
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["kind"] for x in lines] == ["date", "discard"] and json.loads(lines[0])["value"] == "2026-03-26"
    decs.save(con, path, [{"target": f"{a}-p2", "kind": "keep"}, {"target": f"{a}-p3", "kind": "date", "value": "2026-03-20"}],
              "jp", received=RECEIVED)
    decs.save(con, path, [{"target": f"{a}-p3", "kind": "keep"}, {"target": f"{a}-p3", "kind": "discard"},
                          {"target": f"{a}-p3", "kind": "restore"}], "jp", received=RECEIVED)
    e = decs.effective(con, a)
    assert e.doc.date == "2026-03-26" and not e.doc.discarded
    assert e.page(2).discarded and e.page(2).keep                                  # 버린 쪽에 keep 이 있어도 버린 것이다
    assert e.page_date(1) == "2026-03-26" and e.page_date(3) == "2026-03-20"        # 쪽의 결정 > 문서의 결정
    assert not e.page(3).discarded and e.page(3).keep
    assert requested(con, a) == 3
    # 파일에서 다시 읽어도 같다 (seq = 줄 번호)
    assert decs.import_into(con, path)["imported"] == 7
    assert decs.effective(con, a) == e and requested(con, a) == 3                   # 바뀐 것이 없으면 요청하지 않는다


def test_one_bad_item_writes_nothing(db):
    con, path = db
    a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    for items in ([{"target": a, "kind": "discard"}, {"target": f"{b}-p2", "kind": "discard"}],
                  [{"target": a, "kind": "discard"}, {"target": "cccccccccccccccc", "kind": "discard"}],
                  [{"target": a, "kind": "date", "value": "2026-02-30"}],
                  [{"target": a, "kind": "keep"}], [{"target": a, "kind": "?"}]):
        with pytest.raises(decs.DecisionError):
            decs.save(con, path, items, "jp", received=RECEIVED)
    with pytest.raises(decs.DecisionError, match="결정한 사람"):
        decs.save(con, path, [{"target": a, "kind": "discard"}], "", received=RECEIVED)
    assert not path.exists()
    assert con.execute("SELECT COUNT(*) FROM doc_decision").fetchone()[0] == 0 and requested(con, a) == requested(con, b) == 0


def test_broken_last_line_is_skipped_and_the_next_line_starts_fresh(db):
    con, path = db
    a = "aaaaaaaaaaaaaaaa"
    decs.save(con, path, [{"target": a, "kind": "discard"}], "jp", received=RECEIVED)
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"decision_id": "x", "target": "' + a)                      # 쓰다 끊긴 줄
    first = path.read_bytes()
    decs.save(con, path, [{"target": a, "kind": "restore"}], "jp", received=RECEIVED)
    assert path.read_bytes().startswith(first)                               # 앞줄은 바뀌지 않는다
    loaded, skipped = decs.load(path)
    assert skipped == 1 and [(seq, d.kind) for seq, d in loaded] == [(1, "discard"), (3, "restore")]
    assert decs.import_into(con, path) == {"path": str(path), "imported": 2, "skipped": 1}
    assert not decs.effective(con, a).doc.discarded


def test_import_requests_work_when_the_file_brings_new_decisions(db, tmp_path):
    """다른 컴퓨터·백업에서 온 줄로 유효한 결정이 바뀐 문서에는 다시 처리를 요청한다 (처리된 문서가 옛 결정으로 남지 않게)."""
    con, path = db
    a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
    decs.save(con, path, [{"target": a, "kind": "discard"}], "jp", received=RECEIVED)
    other = tmp_path / "다른" / "decisions.jsonl"
    other.parent.mkdir()
    other.write_text(path.read_text(encoding="utf-8")
                     + decs.Decision(b, "date", "2026-03-01", decided_by="jp", decided_at="2026-03-27T01:00:00Z").to_json()
                     + "\n", encoding="utf-8")
    assert (requested(con, a), requested(con, b)) == (1, 0)
    decs.import_into(con, other)
    assert (requested(con, a), requested(con, b)) == (1, 1)
    decs.import_into(con, path)                                              # b 의 결정이 사라졌다 — 그것도 바뀐 것이다
    assert (requested(con, a), requested(con, b)) == (1, 2)
