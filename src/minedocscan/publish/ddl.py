"""대상(PostgreSQL)의 표 정의 (tasks/0008 4.8): schema.sql 한 곳에서 나온다 — 그 파일의 글자를 파싱하지 않고 메모리의 SQLite 에
올려 PRAGMA table_info·index_list 로 만든다. 싣는 표·싣는 열만 (store/db.py 의 PUBLISH_TABLES·PUBLISH_SKIP_COLUMNS).

- 형을 넓힌다: REAL → DOUBLE PRECISION, INTEGER → BIGINT (SQLite 의 REAL 은 8바이트, INTEGER 는 8바이트까지 — 1절 나).
- 외래 키는 걸지 않는다 (문서 단위로 갈아 끼운다 — 맞는지는 작업 DB 가 원본). 기본 키·UNIQUE·인덱스는 건다.
- 상태 표 pub_state(범위의 종류·키 → 지문, 실은 시각)와 pub_meta(스키마 버전·싣기의 판).
DB 에 닿지 않는다 — 글자열만 만든다 (서버 없이 시험한다).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ..store.db import PRIMARY_KEYS, PUBLISH_TABLES, SCHEMA_PATH, publish_columns

TYPES = {"TEXT": "TEXT", "REAL": "DOUBLE PRECISION", "INTEGER": "BIGINT", "BLOB": "BYTEA"}
STATE_TABLE, META_TABLE = "pub_state", "pub_meta"
SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")


@dataclass(frozen=True)
class Column:
    name: str
    type: str          # 대상의 형
    notnull: bool


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    pk: tuple[str, ...]
    uniques: tuple[tuple[str, ...], ...]
    indexes: tuple[tuple[str, tuple[str, ...], bool], ...]     # (이름, 열, UNIQUE 인가)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


def q(name: str) -> str:
    """식별자 (작업 DB 의 표·열 이름 — 영문 소문자·숫자·밑줄뿐이다). 큰따옴표로."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise ValueError(f"식별자로 쓸 수 없는 이름: {name!r}")
    return f'"{name}"'


def check_schema_name(schema: str) -> str:
    if not SCHEMA_RE.match(schema or ""):
        raise ValueError("[publish] schema 는 영문 소문자·숫자·밑줄 (영문 소문자나 밑줄로 시작, 63자 안)")
    return schema


def tables() -> list[Table]:
    """싣는 표의 정의 (PUBLISH_TABLES 의 순서 — 부모부터)."""
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        out = []
        for name in PUBLISH_TABLES:
            keep = publish_columns(name, [r[1] for r in mem.execute(f"PRAGMA table_info({name})")])
            info = {r[1]: r for r in mem.execute(f"PRAGMA table_info({name})")}
            cols = tuple(Column(c, TYPES[(info[c][2] or "TEXT").upper()], bool(info[c][3]) or info[c][5] > 0) for c in keep)
            pk = tuple(c for c in sorted(info, key=lambda c: info[c][5]) if info[c][5] > 0)
            if pk != PRIMARY_KEYS[name]:
                raise RuntimeError(f"{name}: schema.sql 의 기본 키가 PRIMARY_KEYS 와 다릅니다")
            uniques, indexes = [], []
            for _seq, iname, unique, origin, _partial in mem.execute(f"PRAGMA index_list({name})"):
                icols = tuple(r[2] for r in mem.execute(f"PRAGMA index_info({iname})"))
                if origin == "pk" or any(c not in keep for c in icols):
                    continue
                if origin == "u":
                    uniques.append(icols)
                else:
                    indexes.append((iname, icols, bool(unique)))
            out.append(Table(name, cols, pk, tuple(sorted(uniques)), tuple(sorted(indexes))))
        return out
    finally:
        mem.close()


def create_statements(schema: str) -> list[str]:
    """대상에 표를 만드는 문 (IF NOT EXISTS 없이 — 처음 만들 때와 --rebuild 에서만 부른다)."""
    s = q(check_schema_name(schema))
    out = []
    for t in tables():
        parts = [f"{q(c.name)} {c.type}{' NOT NULL' if c.notnull else ''}" for c in t.columns]
        parts.append(f"PRIMARY KEY ({', '.join(q(c) for c in t.pk)})")
        parts += [f"UNIQUE ({', '.join(q(c) for c in u)})" for u in t.uniques]
        out.append(f"CREATE TABLE {s}.{q(t.name)} (\n  " + ",\n  ".join(parts) + "\n)")
        out += [f"CREATE {'UNIQUE ' if uq else ''}INDEX {q(i)} ON {s}.{q(t.name)} ({', '.join(q(c) for c in cols)})"
                for i, cols, uq in t.indexes]
    out.append(f"CREATE TABLE {s}.{q(STATE_TABLE)} (\n  \"kind\" TEXT NOT NULL,\n  \"key\" TEXT NOT NULL,\n"
               f"  \"fingerprint\" TEXT NOT NULL,\n  \"published_at\" TEXT NOT NULL,\n  PRIMARY KEY (\"kind\", \"key\")\n)")
    out.append(f"CREATE TABLE {s}.{q(META_TABLE)} (\n  \"key\" TEXT PRIMARY KEY,\n  \"value\" TEXT NOT NULL\n)")
    return out


def owned_tables() -> list[str]:
    """이 프로그램이 만드는 표 — --rebuild 가 지우는 것은 이것뿐이다 (자식부터)."""
    return [META_TABLE, STATE_TABLE, *reversed(PUBLISH_TABLES)]
