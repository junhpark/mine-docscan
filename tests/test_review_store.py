"""검수 기록: 추가 전용 파일 ↔ doc_review ↔ doc_field. 이미지 없이 도는 부분과 합성 하루치로 도는 부분."""
import json
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
