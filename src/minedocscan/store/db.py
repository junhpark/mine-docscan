"""DB 연결과 쓰기 도우미.

지금은 SQLite 만 구현되어 있다. 스키마와 upsert 문법(INSERT … ON CONFLICT … DO UPDATE)은
PostgreSQL 에서도 그대로 통하도록 골랐으므로, 운영용 어댑터는 연결과 자리표시자(? → %s)만 바꾸면 된다.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# 테이블별 기본 키 (upsert 의 충돌 대상)
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "doc_document": ("document_id",),
    "doc_page": ("page_id",),
    "doc_field": ("field_id",),
    "eq_equipment": ("equipment_id",),
    "eq_assignment_obs": ("work_date", "slot"),
    "insp_daily": ("inspection_id",),
    "prod_haul": ("haul_id",),
    "xcheck_haul": ("work_date", "slot", "material", "level"),
}


def open_db(url: str) -> sqlite3.Connection:
    if not url.startswith("sqlite:///"):
        raise NotImplementedError(f"아직 SQLite 만 지원합니다 (docs/ROADMAP.md): {url}")
    path = url[len("sqlite:///"):]
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


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
