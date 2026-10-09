"""범위와 지문 (tasks/0008 4.8) — 순수 함수: 작업 DB(SQLite)를 읽기만 한다.

범위는 셋이다:
  document  doc_document 의 그 행 + 그 문서의 쪽을 가리키는 행 전부 (doc_page·doc_field·doc_page_meta·prod_haul·prod_tally·
            eq_usage_daily·insp_daily·xcheck_usage — 쪽 ID 로 문서에 붙인다)
  date      날짜가 키인 표 (xcheck_haul, eq_assignment_obs)
  whole     eq_equipment
싣는 표의 모든 행이 이 셋 중 하나에 들어야 한다 — 어디에도 들지 않는 행(쪽 ID 가 없는 행, 없는 쪽을 가리키는 행)이 있으면 싣지 않는다
(Orphans — 수로 알린다. 조용히 빠뜨리지 않는다).

지문 = 그 범위의 행들을 정해진 순서(표의 순서, 표 안에서는 기본 키)와 표현(JSON — 실수는 repr 로 정확히)으로 이은 것의 SHA-256.
행이 들어간 순서와 무관하다. --fresh 로 다시 만든 DB 도 내용이 같으면 지문이 같다 (싣는 열에 시각·요청 번호·로컬 경로가 없다).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from ..store.db import PRIMARY_KEYS, PUBLISH_TABLES, publish_columns

PUBLISH_VERSION = 2      # 범위·표현·표와 열의 목록·pub_meta 의 판 — 바꾸면 올린다 (대상의 판이 다르면 --rebuild).
                         # 2: pub_meta 에 사이트 이름 (tasks/0009 4.1 다)
DOC_TABLES = ("doc_document", "doc_page", "doc_field", "doc_page_meta", "insp_daily", "prod_haul", "eq_usage_daily",
              "prod_tally", "xcheck_usage")
DATE_TABLES = {"eq_assignment_obs": "work_date", "xcheck_haul": "work_date"}
WHOLE_TABLES = ("eq_equipment",)
CHUNK = 500
assert set(DOC_TABLES) | set(DATE_TABLES) | set(WHOLE_TABLES) == set(PUBLISH_TABLES)


class Orphans(RuntimeError):
    """어느 범위에도 들지 않는 행이 있다 — 싣지 않는다. counts: 표 → 행 수 (값 없이)."""

    def __init__(self, counts: dict[str, int]):
        self.counts = counts
        super().__init__("어느 범위에도 들지 않는 행: " + ", ".join(f"{t} {n}" for t, n in sorted(counts.items())))


@dataclass
class Scope:
    kind: str                     # document | date | whole
    key: str                      # 문서 ID | 날짜 | "eq_equipment"
    rows: dict[str, list[tuple]] = field(default_factory=dict)    # 표 → 행들 (싣는 열의 순서, 기본 키로 정렬)

    @property
    def id(self) -> tuple[str, str]:
        return (self.kind, self.key)

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(f"v{PUBLISH_VERSION}|{self.kind}|{self.key}\n".encode())
        for t in sorted(self.rows):
            h.update(f"#{t}\n".encode())
            for r in self.rows[t]:
                h.update(_canon(r))
                h.update(b"\n")
        return h.hexdigest()

    def unsuitable(self) -> int:
        """대상의 형이 받지 않는 값의 수 (NUL 문자가 든 글자 — PostgreSQL 의 TEXT 는 받지 않는다. SQLite 는 저장한다)."""
        return sum(1 for rows in self.rows.values() for r in rows for v in r if isinstance(v, str) and "\x00" in v)


def _canon(row: tuple) -> bytes:
    return json.dumps([_val(v) for v in row], ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _val(v):
    if isinstance(v, float):
        return {"f": repr(v)}          # 실수는 정확한 표현 (정수 1 과 실수 1.0 을 가른다)
    if isinstance(v, bytes):
        return {"b": v.hex()}
    return v


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    return publish_columns(table, [r[1] for r in con.execute(f"PRAGMA table_info({table})")])


def _select(con, table: str, where: str, args: Iterable) -> list[tuple]:
    cols = columns(con, table)
    pk = PRIMARY_KEYS[table]
    rows = [tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where}", tuple(args))]
    idx = [cols.index(c) for c in pk]
    rows.sort(key=lambda r: tuple("" if r[i] is None else str(r[i]) for i in idx))
    return rows


def orphans(con: sqlite3.Connection) -> dict[str, int]:
    """어느 범위에도 들지 않는 행: 쪽을 가리키는 표에서 page_id 가 없거나 없는 쪽을 가리키는 행, 문서가 없는 쪽, 날짜가 없는 날짜 표의 행."""
    out = {}
    for t in DOC_TABLES[2:]:
        n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE page_id IS NULL OR page_id NOT IN (SELECT page_id FROM doc_page)"
                        ).fetchone()[0]
        if n:
            out[t] = n
    n = con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id NOT IN (SELECT document_id FROM doc_document)").fetchone()[0]
    if n:
        out["doc_page"] = n
    for t, c in DATE_TABLES.items():
        n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} IS NULL").fetchone()[0]
        if n:
            out[t] = n
    return out


def all_keys(con: sqlite3.Connection) -> dict[str, set[str]]:
    """작업 DB 에 있는 범위의 키: {"document": {…}, "date": {…}, "whole": {"eq_equipment"}}."""
    dates: set[str] = set()
    for t, c in DATE_TABLES.items():
        dates |= {r[0] for r in con.execute(f"SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL")}
    return {"document": {r[0] for r in con.execute("SELECT document_id FROM doc_document")}, "date": dates,
            "whole": {"eq_equipment"}}


def build(con: sqlite3.Connection, kind: str, keys: Iterable[str]) -> Iterator[Scope]:
    """범위들 (키의 순서대로). 작업 DB 에 없는 키는 행이 없는 범위로 나온다 (대상에서 지울 것)."""
    keys = sorted(set(keys))
    if kind == "whole":
        for k in keys:
            yield Scope("whole", k, {t: _select(con, t, "1 = 1", ()) for t in WHOLE_TABLES})
        return
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i:i + CHUNK]
        marks = ",".join("?" * len(chunk))
        if kind == "date":
            got: dict[str, dict[str, list]] = {k: {} for k in chunk}
            for t, c in DATE_TABLES.items():
                cols = columns(con, t)
                ci = cols.index(c)
                for r in _select(con, t, f"{c} IN ({marks})", chunk):
                    got[r[ci]].setdefault(t, []).append(r)
            for k in chunk:
                yield Scope("date", k, {t: got[k].get(t, []) for t in DATE_TABLES})
            continue
        pages: dict[str, str] = {r[0]: r[1] for r in con.execute(
            f"SELECT page_id, document_id FROM doc_page WHERE document_id IN ({marks})", chunk)}
        got = {k: {t: [] for t in DOC_TABLES} for k in chunk}
        for r in _select(con, "doc_document", f"document_id IN ({marks})", chunk):
            got[r[columns(con, "doc_document").index("document_id")]]["doc_document"].append(r)
        cols_p = columns(con, "doc_page")
        for r in _select(con, "doc_page", f"document_id IN ({marks})", chunk):
            got[r[cols_p.index("document_id")]]["doc_page"].append(r)
        ids = sorted(pages)
        for t in DOC_TABLES[2:]:
            cols = columns(con, t)
            pi = cols.index("page_id")
            for j in range(0, len(ids), CHUNK):
                pc = ids[j:j + CHUNK]
                for r in _select(con, t, f"page_id IN ({','.join('?' * len(pc))})", pc):
                    got[pages[r[pi]]][t].append(r)
        for k in chunk:
            for t in DOC_TABLES:                    # 쪽마다 나눠 읽었으니 범위 안에서 다시 기본 키로 정렬
                idx = [columns(con, t).index(c) for c in PRIMARY_KEYS[t]]
                got[k][t].sort(key=lambda r, idx=idx: tuple("" if r[i] is None else str(r[i]) for i in idx))
            yield Scope("document", k, got[k])
