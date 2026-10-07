"""싣기 (tasks/0008 4.8): 작업 DB(SQLite) → 통합 DB(PostgreSQL). 지문이 다른 범위만 갈아 끼운다 — 한 번의 싣기는 한 트랜잭션.

대상에 쓰는 곳은 이 모듈 하나다 (CLAUDE.md 의 "DB 쓰기는 upsert() 만"은 작업 DB 의 규칙). psycopg 는 connect() 안에서만 import 한다.
연결은 connect 하나를 거친다 — 시험이 바꿔 끼운다 (모듈의 connect 를 바꾸거나 run(connect=…)).
비밀값: URL·비밀번호를 어디에도 찍지 않는다. 오류는 종류(예외의 이름)와 수만 — 드라이버의 오류 글은 싣지 않는다 (비밀번호가 섞일 수 있다).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ..store.db import SCHEMA_VERSION, read_txn
from . import ddl
from .ddl import META_TABLE, STATE_TABLE, q
from .scopes import (
    DATE_TABLES,
    DOC_TABLES,
    PUBLISH_VERSION,
    WHOLE_TABLES,
    Orphans,
    Scope,
    all_keys,
    build,
    orphans,
)

KINDS = ("document", "date", "whole")


class PublishError(RuntimeError):
    """싣지 못했다 — 한 줄 (값·비밀값 없이). code: 종료 코드 (2 = 닿지 못했다·드라이버가 없다·판이 다르다)."""

    def __init__(self, message: str, code: int = 2, kind: str = "error"):
        super().__init__(message)
        self.code, self.kind = code, kind


class NeedRebuild(PublishError):
    def __init__(self, what: str):
        super().__init__(f"대상의 {what} 이(가) 이 프로그램과 다릅니다 — minedocscan publish --rebuild", 2, "version")


@dataclass
class Result:
    replaced: dict[str, int] = field(default_factory=lambda: {k: 0 for k in KINDS})     # 갈아 끼운 범위
    removed: dict[str, int] = field(default_factory=lambda: {k: 0 for k in KINDS})      # 대상에서만 지운 범위
    skipped: int = 0                     # 실을 수 없는 값이 있어 건너뛴 범위 (대상의 옛 행은 지웠다)
    rows: int = 0                        # 넣은 행
    checked: int = 0                     # 지문을 견준 범위
    full: bool = False
    fell_back: bool = False              # 더러운 범위만 싣다가 실패해 전체 훑기로 다시 했다
    created: bool = False                # 대상의 표를 이번에 만들었다
    checked_ids: set = field(default_factory=set, repr=False)    # 견준 범위 (종류, 키) — AutoPublish 가 건너뛴 범위를 들고 있는 데
    skipped_ids: set = field(default_factory=set, repr=False)    # 건너뛴 범위 (종류, 키) — 찍지 않는다 (문서 ID·날짜)

    @property
    def changed(self) -> int:
        return sum(self.replaced.values()) + sum(self.removed.values())

    def as_dict(self) -> dict:
        return {"replaced": dict(self.replaced), "removed": dict(self.removed), "skipped": self.skipped, "rows": self.rows,
                "checked": self.checked, "full": self.full, "fell_back": self.fell_back, "created": self.created}


# ── 연결 ───────────────────────────────────────────────────────────────────
_URL = re.compile(r"(?i)postgres(?:ql)?://")
_NAME = re.compile(r"[A-Za-z0-9._:\-]{1,253}")               # 찍어도 되는 호스트·DB 이름 (그 밖의 글자면 ? — 비밀번호의 조각일 수 있다)
_KW = re.compile(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)")


def _name(v: str | None, missing: str = "?") -> str:
    v = (v or "").strip("'")
    return missing if not v else v if _NAME.fullmatch(v) else "?"


def describe_url(url: str | None) -> str:
    """찍어도 되는 대상의 이름: 호스트(:포트)/DB — 사용자·비밀번호 없이. postgresql://… 꼴과 libpq 의 키워드 꼴(host=… dbname=…).
    그 밖의 글자는 내지 않는다: URL 의 사용자 정보 뒤(경로·질의)에 '@' 가 있으면 비밀번호에 '/'·'?'·'#' 가 그대로 들어간 것이라
    어디까지가 비밀인지 모른다 → 읽을 수 없는 URL. 호스트·DB 이름은 글자·숫자·'.-_' 만 (아니면 ?)."""
    if not url:
        return "(없음)"
    s = url.strip()
    if not _URL.match(s):                                     # 키워드 꼴: 아는 키만 고른다 (password 는 보지 않는다)
        kv = {k.lower(): v for k, v in _KW.findall(s)}
        if not kv:
            return "(읽을 수 없는 연결 문자열)"
        port = (kv.get("port") or "").strip("'")
        return f"{_name(kv.get('host'), 'localhost')}{':' + port if port.isdigit() else ''}/{_name(kv.get('dbname'))}"
    try:
        u = urlsplit(s)
        if "@" in u.path + u.query + u.fragment:
            return "(읽을 수 없는 URL)"
        host = _name(u.hostname, "localhost")
        port = f":{u.port}" if u.port else ""
    except ValueError:
        return "(읽을 수 없는 URL)"
    return f"{host}{port}/{_name(u.path.lstrip('/'))}"


def scrub(text: str, url: str | None) -> str:
    """글에서 URL·비밀번호를 지운다 (안전장치 — 드라이버의 글을 찍지 않는 것이 먼저다). URL 꼴이면 '://' 와 마지막 '@' 사이 전부를
    비밀로 본다 (비밀번호에 '/' 가 그대로 들어가 urlsplit 이 잘못 가르는 때도)."""
    if not url:
        return text
    out = text.replace(url, describe_url(url))
    secrets = []
    m = _URL.match(url.strip())
    if m and "@" in url:
        info = url.strip()[m.end():url.strip().rindex("@")]
        secrets += [info, info.split(":", 1)[-1]]
    try:
        secrets.append(urlsplit(url).password)
    except ValueError:
        pass
    for sec in sorted({x for x in secrets if x and len(x) >= 3}, key=len, reverse=True):
        out = out.replace(sec, "***")
    return re.sub(r"password\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", "password=***", out)


def psycopg_connect(url: str, timeout_s: float):
    """psycopg 연결 (autocommit 꺼짐). 드라이버가 없으면 PublishError (종료 코드 2)."""
    try:
        import psycopg
    except ImportError:
        raise PublishError("psycopg 가 없습니다 — pip install -e \".[postgres]\" (싣기는 꺼집니다)", 2, "no_driver") from None
    return psycopg.connect(url, connect_timeout=max(1, round(timeout_s)), autocommit=False)


connect = psycopg_connect      # 연결은 이 이름 하나를 거친다 — 시험이 바꿔 끼운다 (부를 때 찾는다)


def _connect_fn(fn: Callable | None) -> Callable:
    return fn if fn is not None else globals()["connect"]


# ── 대상 ───────────────────────────────────────────────────────────────────
class Target:
    """대상의 스키마 하나 (연결 하나). 이 클래스만 대상에 SQL 을 보낸다."""

    def __init__(self, conn, schema: str):
        self.conn, self.schema = conn, ddl.check_schema_name(schema)
        self.s = q(self.schema)

    def cur(self):
        return self.conn.cursor()

    def exists(self) -> bool:
        with self.cur() as c:
            c.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                      (self.schema, META_TABLE))
            return c.fetchone()[0] > 0

    def versions(self) -> dict[str, str]:
        with self.cur() as c:
            c.execute(f"SELECT key, value FROM {self.s}.{q(META_TABLE)}")
            return dict(c.fetchall())

    def create(self) -> None:
        """표를 만든다. 스키마는 없을 때만 만든다 — CREATE SCHEMA 는 (IF NOT EXISTS 여도) DB 의 CREATE 권한이 있어야 하므로, 관리자가
        스키마를 만들어 준 전용 계정은 그 스키마 안에만 쓴다."""
        with self.cur() as c:
            c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (self.schema,))
            if c.fetchone() is None:
                c.execute(f"CREATE SCHEMA {self.s}")
            for stmt in ddl.create_statements(self.schema):
                c.execute(stmt)
            c.executemany(f"INSERT INTO {self.s}.{q(META_TABLE)} (key, value) VALUES (%s, %s)",
                          [("schema_version", str(SCHEMA_VERSION)), ("publish_version", str(PUBLISH_VERSION))])

    def drop(self) -> None:
        """이 프로그램이 만든 표만 지운다 (CASCADE 하지 않는다 — 뷰가 걸려 있으면 드라이버가 거절하고, 부른 쪽이 알린다)."""
        with self.cur() as c:
            for t in ddl.owned_tables():
                c.execute(f"DROP TABLE IF EXISTS {self.s}.{q(t)}")

    def state(self, kind: str | None = None, keys: Iterable[str] | None = None) -> dict[tuple[str, str], str]:
        with self.cur() as c:
            if kind is None:
                c.execute(f"SELECT kind, key, fingerprint FROM {self.s}.{q(STATE_TABLE)}")
                return {(k, key): fp for k, key, fp in c.fetchall()}
            keys = sorted(set(keys or ()))
            if not keys:
                return {}
            c.execute(f"SELECT kind, key, fingerprint FROM {self.s}.{q(STATE_TABLE)} WHERE kind = %s AND key = ANY(%s)",
                      (kind, keys))
            return {(k, key): fp for k, key, fp in c.fetchall()}

    def documents_on(self, dates: Iterable[str]) -> set[str]:
        """대상에서 그 날짜에 쪽이 있는 문서 (더러운 날짜의 문서 — 대상에도 doc_page.work_date 가 있다)."""
        dates = sorted(set(dates))
        if not dates:
            return set()
        with self.cur() as c:
            c.execute(f"SELECT DISTINCT document_id FROM {self.s}.\"doc_page\" WHERE work_date = ANY(%s)", (dates,))
            return {r[0] for r in c.fetchall()}

    def delete_scope(self, kind: str, key: str) -> None:
        """그 범위의 행을 지운다 (자식부터)."""
        with self.cur() as c:
            if kind == "document":
                pages = f"SELECT page_id FROM {self.s}.\"doc_page\" WHERE document_id = %s"
                for t in reversed(DOC_TABLES[2:]):
                    c.execute(f"DELETE FROM {self.s}.{q(t)} WHERE page_id IN ({pages})", (key,))
                c.execute(f"DELETE FROM {self.s}.\"doc_page\" WHERE document_id = %s", (key,))
                c.execute(f"DELETE FROM {self.s}.\"doc_document\" WHERE document_id = %s", (key,))
            elif kind == "date":
                for t, col in DATE_TABLES.items():
                    c.execute(f"DELETE FROM {self.s}.{q(t)} WHERE {q(col)} = %s", (key,))
            else:
                for t in WHOLE_TABLES:
                    c.execute(f"DELETE FROM {self.s}.{q(t)}")
            c.execute(f"DELETE FROM {self.s}.{q(STATE_TABLE)} WHERE kind = %s AND key = %s", (kind, key))

    def insert(self, table: str, cols: list[str], rows: list[tuple]) -> None:
        if not rows:
            return
        with self.cur() as c:
            copy = getattr(c, "copy", None)
            if copy is not None:                         # psycopg 3: COPY 가 가장 빠르다 (실수는 정확한 표현으로 간다)
                with copy(f"COPY {self.s}.{q(table)} ({', '.join(q(x) for x in cols)}) FROM STDIN") as cp:
                    for r in rows:
                        cp.write_row(r)
            else:
                c.executemany(f"INSERT INTO {self.s}.{q(table)} ({', '.join(q(x) for x in cols)}) VALUES "
                              f"({', '.join(['%s'] * len(cols))})", rows)

    def set_state(self, items: list[tuple[str, str, str]], at: str) -> None:
        if not items:
            return
        with self.cur() as c:
            c.executemany(f"INSERT INTO {self.s}.{q(STATE_TABLE)} (kind, key, fingerprint, published_at) VALUES (%s, %s, %s, %s)",
                          [(k, key, fp, at) for k, key, fp in items])


# ── 싣기 ───────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan(con, target: Target, full: bool, documents: Iterable[str] = (), dates: Iterable[str] = ()) -> dict[str, set[str]]:
    """견줄 범위의 키: full 이면 작업 DB 와 대상의 전부, 아니면 준 문서 + 더러운 날짜에 쪽이 있는 문서(양쪽) + 날짜 + 통째."""
    if full:
        keys = all_keys(con)
        for (kind, key) in target.state():
            keys.setdefault(kind, set()).add(key)
        return keys
    dates = {d for d in dates if d}
    docs = set(documents) | target.documents_on(dates)
    for i in range(0, len(sorted(dates)), 500):
        chunk = sorted(dates)[i:i + 500]
        docs |= {r[0] for r in con.execute(
            f"SELECT DISTINCT document_id FROM doc_page WHERE work_date IN ({','.join('?' * len(chunk))})", chunk)}
    return {"document": docs, "date": dates, "whole": {"eq_equipment"}}


def publish(con, target: Target, full: bool = True, documents: Iterable[str] = (), dates: Iterable[str] = (),
            check: bool = False, now: Callable[[], str] = _now, fail_after: int | None = None) -> Result:
    """한 번의 싣기 (한 트랜잭션). check=True 면 쓰지 않고(상태 표도) 다른 범위의 수만 Result 에.
    작업 DB 는 한 읽기 트랜잭션으로 본다 (store.db.read_txn). fail_after: 시험용 — 그 수의 범위를 넣은 뒤 예외 (한 트랜잭션인지)."""
    res = Result(full=full)
    with read_txn(con):
        bad = orphans(con)
        if bad:
            raise Orphans(bad)
        if not target.exists():
            if check:
                keys = all_keys(con)
                res.replaced = {k: len(keys[k]) for k in KINDS}
                return res
            target.create()
            res.created = True
        else:
            v = target.versions()
            if v.get("schema_version") != str(SCHEMA_VERSION):
                raise NeedRebuild("스키마 버전")
            if v.get("publish_version") != str(PUBLISH_VERSION):
                raise NeedRebuild("싣기의 판")
        keys = plan(con, target, full, documents, dates)
        local = all_keys(con)
        todo: list[tuple[Scope, str]] = []
        gone: list[tuple[str, str]] = []
        for kind in KINDS:
            have = target.state(None) if full else target.state(kind, keys.get(kind, ()))
            for sc in build(con, kind, keys.get(kind, ())):
                res.checked += 1
                res.checked_ids.add(sc.id)
                old = have.get(sc.id)
                if sc.key not in local[kind]:
                    if old is not None:                      # 대상에 있던 범위만 (실을 수 없어 건너뛴 범위는 행도 상태도 없다)
                        gone.append(sc.id)
                    continue
                fp = sc.fingerprint()
                if old != fp:
                    todo.append((sc, fp))
        if check:
            for sc, _fp in todo:
                res.replaced[sc.kind] += 1
            for kind, _key in gone:
                res.removed[kind] += 1
            return res
        # 지울 것을 먼저 다 지우고 넣는다 — 같은 키의 행이 범위 사이를 옮겨 다닌다 (점검의 이기는 쪽, 일보의 자리)
        for kind, key in gone:
            target.delete_scope(kind, key)
            res.removed[kind] += 1
        for sc, _fp in todo:
            target.delete_scope(sc.kind, sc.key)
        at = now()
        states = []
        for n, (sc, fp) in enumerate(todo):
            if fail_after is not None and n >= fail_after:
                raise RuntimeError("시험: 싣는 가운데의 실패")
            if sc.unsuitable():
                res.skipped += 1                         # 옛 행은 위에서 지웠다. 상태 표에도 없다 — 다음에도 "다르다"
                res.skipped_ids.add(sc.id)
                continue
            for t, rows in sc.rows.items():
                target.insert(t, _cols(con, t), rows)
                res.rows += len(rows)
            states.append((sc.kind, sc.key, fp))
            res.replaced[sc.kind] += 1
        target.set_state(states, at)
    return res


def _cols(con, table: str) -> list[str]:
    from .scopes import columns

    return columns(con, table)


# ── 한 번의 싣기 (연결부터 커밋까지) ───────────────────────────────────────────────
def open_target(settings, connect: Callable | None = None):
    """대상에 연결한다. 실패하면 PublishError(종류 connect) — 드라이버의 글은 싣지 않는다 (비밀번호가 섞일 수 있다)."""
    if not settings.publish_url:
        raise PublishError("통합 DB 가 없습니다: 환경변수 MINEDOCSCAN_PUBLISH_URL", 2, "off")
    try:
        conn = _connect_fn(connect)(settings.publish_url, settings.publish_connect_timeout_s)
    except PublishError:
        raise
    except Exception as e:                                  # noqa: BLE001 — 연결 실패의 종류만
        raise PublishError(f"통합 DB({describe_url(settings.publish_url)})에 닿지 못했습니다 ({type(e).__name__})", 2,
                           "connect") from None
    return Target(conn, settings.publish_schema)


def _is_conflict(e: BaseException) -> bool:
    """키가 부딪쳤다 (더러운 범위만 실었는데 빠뜨린 범위가 그 키를 쥐고 있었다) — psycopg 의 IntegrityError (SQLSTATE 23…)."""
    state = getattr(e, "sqlstate", None) or getattr(getattr(e, "diag", None), "sqlstate", None)
    return type(e).__name__ in ("IntegrityError", "UniqueViolation") or (isinstance(state, str) and state.startswith("23"))


def run(con, settings, full: bool = True, documents: Iterable[str] = (), dates: Iterable[str] = (), check: bool = False,
        connect: Callable | None = None, target: Target | None = None, fail_after: int | None = None,
        rebuild: bool = False) -> Result:
    """연결 → 한 트랜잭션으로 싣기 → 커밋. 더러운 범위만 실은 것이 키 충돌로 실패하면 되돌리고 같은 자리에서 전체 훑기로 다시 한다.
    실패하면 되돌리고 PublishError (종류와 수만). check=True 면 쓰지 않는다 (되돌린다). rebuild=True 면 이 프로그램의 표를 지우고
    다시 만든 뒤 싣는다 — 같은 트랜잭션이라 읽는 쪽은 빈 표를 보지 않고, 실패하면 대상은 그 전 그대로다."""
    own = target is None
    t = target or open_target(settings, connect)
    try:
        if rebuild:
            _drop_and_create(t)
        try:
            res = publish(con, t, full=full, documents=documents, dates=dates, check=check, fail_after=fail_after)
        except Exception as e:
            _rollback(t)
            if full or check or rebuild or not _is_conflict(e):
                raise
            res = publish(con, t, full=True, check=False, fail_after=fail_after)
            res.fell_back = True
        if check:
            _rollback(t)
        else:
            t.conn.commit()
        return res
    except (PublishError, Orphans):
        _rollback(t)
        raise
    except Exception as e:                                   # noqa: BLE001 — 드라이버의 글을 싣지 않는다
        _rollback(t)
        raise PublishError(f"통합 DB 에 싣지 못했습니다 ({type(e).__name__}) — 대상은 싣기 전 그대로입니다", 2,
                           type(e).__name__) from None
    finally:
        if own:
            try:
                t.conn.close()
            except Exception:                                # noqa: BLE001
                pass


def _drop_and_create(t: Target) -> None:
    """이 프로그램이 만든 표만 지우고 다시 만든다 (CASCADE 하지 않는다 — 부른 쪽의 트랜잭션 안에서, 커밋하지 않는다).
    뷰가 걸려 있어 지우지 못하면 PublishError (종류 depends)."""
    try:
        t.drop()
        t.create()
    except Exception as e:                                   # noqa: BLE001
        _rollback(t)
        if type(e).__name__ == "DependentObjectsStillExist" or getattr(e, "sqlstate", None) == "2BP01":
            raise PublishError("대상의 표에 다른 것(뷰 등)이 걸려 있어 지우지 않았습니다 — 그것을 먼저 치우십시오", 2,
                               "depends") from None
        raise PublishError(f"다시 만들지 못했습니다 ({type(e).__name__})", 2, type(e).__name__) from None


def _rollback(t: Target) -> None:
    try:
        t.conn.rollback()
    except Exception:                                        # noqa: BLE001 — 끊긴 연결
        pass
