"""DB 연결과 쓰기 도우미.

지금은 SQLite 만 구현되어 있다. 스키마와 upsert 문법(INSERT … ON CONFLICT … DO UPDATE)은
PostgreSQL 에서도 그대로 통하도록 골랐으므로, 운영용 어댑터는 연결과 자리표시자(? → %s)만 바꾸면 된다.

스키마 버전: 컬럼이 바뀌면 SCHEMA_VERSION 을 올린다. 마이그레이션은 만들지 않는다 (ADR 0005).
버전이 다른 DB 파일은 열지 않고 SchemaVersionError 를 낸다 — `run --fresh` 로 다시 만들면 된다.
사람이 입력한 값(검수)은 사이트 팩의 파일에 있으므로 DB 를 지워도 잃지 않는다 (review/store.py).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# 1: 처음 골격. 2: 검수(doc_review, has_value_raw, trips_raw, *_trips_raw) — docs/tasks/0001-review-tool.md
# 3: 전체 묶음 운용(prod_haul.has_value_raw, 오류 격리, 호모그래피 …) — docs/tasks/0002-full-archive-and-evalset.md
# 4: 숫자 인식기(doc_document.warning, doc_field.status_raw) — docs/tasks/0003-digit-recognizer.md
# 5: 쪽 메타의 출처(doc_page_meta) — docs/tasks/0004-page-fields-and-checks.md
# 6: 값의 형식(doc_field.format), 장비 가동 일보(eq_usage_daily, prod_tally, xcheck_usage) — docs/tasks/0005-usage-logs.md
SCHEMA_VERSION = 6

# 테이블별 기본 키 (upsert 의 충돌 대상)
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "meta_schema": ("key",),
    "doc_document": ("document_id",),
    "doc_page": ("page_id",),
    "doc_page_meta": ("page_id", "meta_key"),
    "doc_field": ("field_id",),
    "doc_review": ("review_id",),
    "eq_equipment": ("equipment_id",),
    "eq_assignment_obs": ("work_date", "slot"),
    "insp_daily": ("inspection_id",),
    "prod_haul": ("haul_id",),
    "xcheck_haul": ("work_date", "slot", "material", "level"),
}


class SchemaVersionError(RuntimeError):
    pass


def open_db(url: str) -> sqlite3.Connection:
    if not url.startswith("sqlite:///"):
        raise NotImplementedError(f"아직 SQLite 만 지원합니다 (docs/ROADMAP.md): {url}")
    path = url[len("sqlite:///"):]
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: 검수 서버(review/server.py)는 요청을 받는 스레드에서 이 연결을 쓴다.
    # 동시에 여러 스레드가 쓰지는 않는다 (서버는 단일 스레드).
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    _check_version(con, path)
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    upsert(con, "meta_schema", {"key": "schema_version", "value": str(SCHEMA_VERSION)})
    con.commit()
    return con


def _check_version(con: sqlite3.Connection, path: str) -> None:
    """스키마를 만들기 전에 본다: 테이블이 이미 있는 DB 는 버전이 같아야 한다 (빈 DB 는 새로 만든다)."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        return
    found = None
    if "meta_schema" in tables:
        row = con.execute("SELECT value FROM meta_schema WHERE key='schema_version'").fetchone()
        found = row[0] if row else None
    if found != str(SCHEMA_VERSION):
        con.close()
        raise SchemaVersionError(
            f"DB 스키마 버전이 다릅니다: 파일 {found or '1 (버전 기록 이전)'}, 코드 {SCHEMA_VERSION} — {path}\n"
            "`minedocscan run --fresh` 로 DB 를 다시 만드세요. 검수 기록은 사이트 팩의 reviews.jsonl 에 있으므로 "
            "다시 돌리면 그대로 붙습니다.")


def upsert(con: sqlite3.Connection, table: str, rows: dict | Iterable[dict]) -> int:
    """기본 키가 같으면 덮어쓴다. 같은 문서를 다시 돌려도 행이 늘지 않는다(멱등)."""
    if isinstance(rows, dict):
        rows = [rows]
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    keys = PRIMARY_KEYS[table]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT ({', '.join(keys)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING"))
    con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)
