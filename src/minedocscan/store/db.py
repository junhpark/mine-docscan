"""DB 연결과 쓰기 도우미.

지금은 SQLite 만 구현되어 있다. 스키마와 upsert 문법(INSERT … ON CONFLICT … DO UPDATE)은
PostgreSQL 에서도 그대로 통하도록 골랐으므로, 운영용 어댑터는 연결과 자리표시자(? → %s)만 바꾸면 된다.

스키마 버전: 컬럼이 바뀌면 SCHEMA_VERSION 을 올린다. 마이그레이션은 만들지 않는다 (ADR 0005).
버전이 다른 DB 파일은 열지 않고 SchemaVersionError 를 낸다 — `run --fresh` 로 다시 만들면 된다.
사람이 입력한 값(검수)은 사이트 팩의 파일에 있으므로 DB 를 지워도 잃지 않는다 (review/store.py).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# 1: 처음 골격. 2: 검수(doc_review, has_value_raw, trips_raw, *_trips_raw) — docs/tasks/0001-review-tool.md
# 3: 전체 묶음 운용(prod_haul.has_value_raw, 오류 격리, 호모그래피 …) — docs/tasks/0002-full-archive-and-evalset.md
# 4: 숫자 인식기(doc_document.warning, doc_field.status_raw) — docs/tasks/0003-digit-recognizer.md
# 5: 쪽 메타의 출처(doc_page_meta) — docs/tasks/0004-page-fields-and-checks.md
# 6: 값의 형식(doc_field.format), 장비 가동 일보(eq_usage_daily, prod_tally, xcheck_usage) — docs/tasks/0005-usage-logs.md
# 7: 인쇄 층·동시 판(doc_page.print_sha, doc_page.variant_errs) — docs/tasks/0006-print-layer-and-variants.md
# 8: 접수(doc_document.received_at·date_source·work_requested·work_done, doc_page.rotation·duplicate_of·duplicate_sim,
#    insp_daily.page_id, doc_decision, doc_page_sig) — docs/tasks/0007-intake.md
SCHEMA_VERSION = 8

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
    "eq_usage_daily": ("page_id",),
    "prod_tally": ("tally_id",),
    "xcheck_usage": ("page_id", "check_kind", "item"),
    "doc_decision": ("decision_id",),
    "doc_page_sig": ("page_id",),
}

# 문서가 만든 것 — 쪽을 가리키는 테이블 (page_id 열), 자식부터 (PostgreSQL 의 외래 키 순서). 문서를 다시 처리할 때 이것을 지우고
# 다시 만든다 (tasks/0007 4.8). 새 업무 테이블을 더하면 여기에도 더한다. doc_review·doc_decision 은 지우지 않는다 (사람이 정한 것).
# 날짜로 만드는 것(xcheck_haul, eq_assignment_obs)은 쪽을 가리키지 않는다 — 그 날짜를 다시 계산해 지운다 (핸들러의 finalize).
PAGE_TABLES: tuple[str, ...] = ("xcheck_usage", "prod_tally", "eq_usage_daily", "prod_haul", "insp_daily", "doc_page_sig",
                                "doc_page_meta", "doc_field", "doc_page")
BUSY_TIMEOUT_S = 30         # 다른 연결이 쓰는 동안 기다리는 시간 — 쓰는 단위가 쪽 하나(1–2초)라 넉넉하다 (4.8)


class SchemaVersionError(RuntimeError):
    pass


def open_db(url: str) -> sqlite3.Connection:
    if not url.startswith("sqlite:///"):
        raise NotImplementedError(f"아직 SQLite 만 지원합니다 (docs/ROADMAP.md): {url}")
    path = url[len("sqlite:///"):]
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: 검수 서버(review/server.py)는 요청을 받는 스레드에서 이 연결을 쓴다.
    # 연결 하나를 여러 스레드가 같이 쓰지는 않는다 (serve 는 스레드마다 연결 하나 — tasks/0007 4.9).
    con = sqlite3.connect(path, check_same_thread=False, timeout=BUSY_TIMEOUT_S)
    con.row_factory = sqlite3.Row
    _check_version(con, path)
    if path != ":memory:":                       # WAL: 읽는 연결(화면)이 쓰는 연결(작업)을 기다리지 않는다. 버전 검사 뒤에 (거절할 파일을 고치지 않게)
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    upsert(con, "meta_schema", {"key": "schema_version", "value": str(SCHEMA_VERSION)})
    con.commit()
    return con


def open_db_readonly(url: str) -> sqlite3.Connection:
    """있는 DB 를 읽기 전용으로 연다 (만들지도, 스키마를 쓰지도 않는다) — DB 에 쓰지 않는 도구용 (template print-layer).
    파일이 없으면 FileNotFoundError, 버전이 다르면 SchemaVersionError."""
    if not url.startswith("sqlite:///"):
        raise NotImplementedError(f"아직 SQLite 만 지원합니다 (docs/ROADMAP.md): {url}")
    path = Path(url[len("sqlite:///"):])
    if not path.is_file():
        raise FileNotFoundError(f"DB 가 없습니다: {path}")
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    _check_version(con, str(path))
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


@contextmanager
def write_txn(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """쓰는 트랜잭션 하나: BEGIN IMMEDIATE … COMMIT (예외면 ROLLBACK). 읽고 나서 쓰기로 올라가는 트랜잭션은 그 사이 다른 연결이
    커밋하면 기다리지 않고 바로 실패한다 — 그래서 쓰는 단위는 처음부터 쓰기 잠금을 잡는다 (tasks/0007 4.8).
    이미 열린 트랜잭션 안이면(부른 쪽의 단위) 새로 열지도 커밋하지도 않는다 — 그 단위가 커밋한다."""
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        if con.in_transaction:
            con.rollback()
        raise
    if con.in_transaction:
        con.commit()


@contextmanager
def read_txn(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """읽는 트랜잭션 하나: BEGIN … (끝나면 되돌린다 — 아무것도 쓰지 않는다). 그 안의 읽기는 한 시점을 본다 (WAL) — 표마다 다른
    시점을 보지 않게 (내보내기·싣기 — tasks/0008 4.1). 이미 열린 트랜잭션 안이면 그것을 그대로 쓴다."""
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN")
    try:
        yield con
    finally:
        if con.in_transaction:
            con.rollback()


def delete_pages(con: sqlite3.Connection, page_ids: list[str]) -> int:
    """그 쪽들이 만든 행을 지운다 (PAGE_TABLES, 자식부터). 돌려주는 값: 지운 쪽 수."""
    ids = sorted(set(page_ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for t in PAGE_TABLES:
            con.execute(f"DELETE FROM {t} WHERE page_id IN ({marks})", chunk)
    return len(ids)


def upsert(con: sqlite3.Connection, table: str, rows: dict | Iterable[dict], insert_only: tuple[str, ...] = ()) -> int:
    """기본 키가 같으면 덮어쓴다. 같은 문서를 다시 돌려도 행이 늘지 않는다(멱등).
    insert_only: 새 행일 때만 쓰고 있는 행에서는 덮어쓰지 않는 열 (doc_document.received_at — 다시 처리해도 받은 시각은 그대로)."""
    if isinstance(rows, dict):
        rows = [rows]
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    keys = PRIMARY_KEYS[table]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys and c not in insert_only)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT ({', '.join(keys)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING"))
    con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)
