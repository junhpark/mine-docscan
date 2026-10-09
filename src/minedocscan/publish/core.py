"""싣기 (tasks/0008 4.8): 작업 DB(SQLite) → 통합 DB(PostgreSQL). 지문이 다른 범위만 갈아 끼운다 — 한 번의 싣기는 한 트랜잭션.

대상에 쓰는 곳은 이 모듈 하나다 (CLAUDE.md 의 "DB 쓰기는 upsert() 만"은 작업 DB 의 규칙). psycopg 는 connect() 안에서만 import 한다.
연결은 connect 하나를 거친다 — 시험이 바꿔 끼운다 (모듈의 connect 를 바꾸거나 run(connect=…)).
비밀값: URL·비밀번호를 어디에도 찍지 않는다. 오류는 종류(예외의 이름)와 수만 — 드라이버의 오류 글은 싣지 않는다 (비밀번호가 섞일 수 있다).
"""
from __future__ import annotations

import contextlib
import re
import socket
import threading
import time
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
    all_keys,
    build,
    local_keys,
    orphans,
)

KINDS = ("document", "date", "whole")


AGAIN = "다음에 다시 합니다"                       # 바퀴 끝의 싣기 — 명령(publish)은 "다시 실행하십시오" (PublishError.for_command)


class PublishError(RuntimeError):
    """싣지 못했다 — 한 줄 (값·비밀값 없이). code: 종료 코드 (2 = 닿지 못했다·드라이버가 없다·판이 다르다·사이트가 다르다)."""

    def __init__(self, message: str, code: int = 2, kind: str = "error"):
        super().__init__(message)
        self.code, self.kind = code, kind

    def for_command(self) -> str:
        """명령이 찍는 글: 시간 제한·끊김의 '다음에 …'는 바퀴 끝의 싣기의 말이다 — 명령은 다시 하지 않는다."""
        return str(self).replace(AGAIN, "다시 실행하십시오")


class NeedRebuild(PublishError):
    def __init__(self, what: str):
        super().__init__(f"대상의 {what} 이(가) 이 프로그램과 다릅니다 — minedocscan publish --rebuild", 2, "version")


# 사본의 주인 (tasks/0009 4.1 다): 실제 DB 를 실어 둔 스키마에 합성 묶음의 작업 폴더로 publish 하면 문서 3·날짜 3 범위를 지우고 합성 행으로
# 바꿨다 (한 트랜잭션, 묻지 않는다 — URL 이 환경변수에 있는 PC 에서 설명서의 합성 예제를 돌리면 일어난다). 대상의 pub_meta 에 사이트 팩의
# [site] name 을 두고 다르면 싣지 않는다. 글에 두 이름을 찍지 않는다
OTHER_SITE = ("대상은 다른 사이트 팩의 사본입니다 — 맞는 대상인지 확인하고, 이 사이트로 바꾸려면 minedocscan publish --rebuild")
NO_SITE_NAME = ("사이트 팩의 site.toml 에 [site] name 이 없습니다 — 통합 DB·엑셀 폴더가 어느 사이트의 사본인지 적는 이름입니다 "
                "(폴더 이름으로 대신하지 않습니다). 적기 전에 minedocscan info 가 평가셋의 소금값을 알리면 그것부터 적습니다")
NOT_OURS = ("대상 스키마에 이 프로그램의 표와 이름이 같은 표가 있지만 pub_meta 가 없습니다 — 이 프로그램이 만든 표가 아니라 지우지 않았습니다. "
            "다른 스키마([publish] schema)를 쓰거나 그 표를 먼저 치우십시오")


def site_name(settings, site: str | None = None) -> str:
    """싣는 쪽의 사이트 이름: 준 것, 없으면 사이트 팩의 [site] name. 둘 다 없으면 PublishError (종류 no_site_name — 연결하기 전에)."""
    if site is None and settings is not None:
        from ..forms.sitepack import declared_site_name

        site = declared_site_name(settings.site)
    if not site:
        raise PublishError(NO_SITE_NAME, 2, "no_site_name")
    return site


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
    slices: list | None = field(default=None, repr=False)       # cycle=True 일 때 이 바퀴의 조각 (첫 조각은 이번에 했다)

    @property
    def changed(self) -> int:
        return sum(self.replaced.values()) + sum(self.removed.values())

    def as_dict(self) -> dict:
        return {"replaced": dict(self.replaced), "removed": dict(self.removed), "skipped": self.skipped, "rows": self.rows,
                "checked": self.checked, "full": self.full, "fell_back": self.fell_back, "created": self.created}


# ── 연결 ───────────────────────────────────────────────────────────────────
_URL = re.compile(r"(?i)postgres(?:ql)?://")
_NAME = re.compile(r"[A-Za-z0-9._:\-]{1,253}")               # 찍어도 되는 호스트·DB 이름 (그 밖의 글자면 ? — 비밀번호의 조각일 수 있다)
_KW_NAME = re.compile(r"[A-Za-z0-9._\-]{1,253}")             # 키워드 꼴의 host·dbname — ':' 도 ? (이름:비밀 을 잘못 적었을 수 있다)
_KW = re.compile(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)")


def _name(v: str | None, missing: str = "?", ok: re.Pattern = _NAME) -> str:
    v = (v or "").strip("'")
    return missing if not v else v if ok.fullmatch(v) else "?"


def describe_url(url: str | None) -> str:
    """찍어도 되는 대상의 이름: 호스트(:포트)/DB — 사용자·비밀번호 없이. postgresql://… 꼴과 libpq 의 키워드 꼴(host=… dbname=…).
    그 밖의 글자는 내지 않는다: URL 의 사용자 정보 뒤(경로·질의)에 '@' 가 있으면 비밀번호에 '/'·'?'·'#' 가 그대로 들어간 것이라
    어디까지가 비밀인지 모른다 → 읽을 수 없는 URL. 사용자 정보('@')가 없는데 호스트 뒤가 숫자 포트가 아니면(postgresql://이름:비밀/db —
    '@호스트'를 빠뜨렸다) 읽을 수 없는 URL. 호스트·DB 이름은 글자·숫자·'.-_' 만 (아니면 ? — 키워드 꼴은 ':' 도 ?)."""
    if not url or not url.strip():
        return "(없음)"
    s = url.strip()
    if not _URL.match(s):                                     # 키워드 꼴: 아는 키만 고른다 (password 는 보지 않는다)
        kv = {k.lower(): v for k, v in _KW.findall(s)}
        if not kv:
            return "(읽을 수 없는 연결 문자열)"
        port = (kv.get("port") or "").strip("'")
        return (f"{_name(kv.get('host'), 'localhost', _KW_NAME)}{':' + port if port.isdigit() else ''}/"
                f"{_name(kv.get('dbname'), ok=_KW_NAME)}")
    try:
        u = urlsplit(s)
        if "@" in u.path + u.query + u.fragment:
            return "(읽을 수 없는 URL)"
        if "@" not in u.netloc:
            after = u.netloc[u.netloc.find("]") + 1:] if u.netloc.startswith("[") else u.netloc[u.netloc.find(":"):] \
                if ":" in u.netloc else ""
            if after and not re.fullmatch(r":\d+", after, re.ASCII):
                return "(읽을 수 없는 URL)"
        host = _name(u.hostname, "localhost")
        host = f"[{host}]" if ":" in host else host               # IPv6 는 괄호로 (포트와 갈리게)
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


# 연결의 TCP keepalive (PR #15 검토): 싣는 가운데 서버의 전원이 나가거나 선이 끊기면 문장의 시간 제한(서버가 건다)은 소용이 없고,
# 연결은 OS 의 기본값(keepalive 2시간, 다시 보내기 약 15분)까지 답을 기다린다 — 그동안 작업 스레드가 선다.
# - 답을 기다릴 때(보낸 것은 받았다고 했다): 10초 조용하면 2초 간격으로 묻는다. 윈도우(현장 PC)는 keepalives_count 를 듣지 않고 10번
#   물으므로 약 30초(10 + 2 × 10)에 끊는다. 리눅스는 tcp_user_timeout 을 걸면 횟수 대신 그 시간(아래)에 끊는다. 서버가 긴 문장을 도는
#   동안은 서버의 커널이 답하므로 끊기지 않는다.
# - 보내는 가운데(COPY): 보낸 것을 받았다고 하지 않은 채 tcp_user_timeout 이 지나면 끊는다 (리눅스만 — 윈도우는 다시 보내기의 기본값). 서버가
#   잠금·느린 문장을 기다리는 동안은 COPY 를 읽지 않아 창이 닫혀도 끊긴다 — 그래서 이 값은 두 시간 제한 중 긴 것보다 30초 길다
#   (`user_timeout_ms`, 기본 90초. 30초로 두었을 때 문서 크기의 COPY 가 잠금 제한 50초·느린 트리거 40초보다 먼저 30초에 끊겨 종류가
#   OperationalError 였다 — 검토의 재현).
# URL 에 적은 값이 있으면 그것을 쓴다.
KEEPALIVE = {"keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 2, "keepalives_count": 3}
USER_TIMEOUT_MARGIN_S = 30


def user_timeout_ms(settings) -> int:
    """tcp_user_timeout (밀리초): 잠금·문장 시간 제한 중 긴 것 + 30초 — 서버의 시간 제한이 먼저 알리게 (기본 90초)."""
    return round((max(_limits(settings)) + USER_TIMEOUT_MARGIN_S) * 1000)


def psycopg_connect(url: str, timeout_s: float, user_timeout_ms: int | None = None):
    """psycopg 연결 (autocommit 꺼짐, TCP keepalive, tcp_user_timeout). 드라이버가 없으면 PublishError (종료 코드 2)."""
    try:
        import psycopg
    except ImportError:
        raise PublishError("psycopg 가 없습니다 — pip install -e \".[postgres]\" (싣기는 꺼집니다)", 2, "no_driver") from None
    try:
        from psycopg.conninfo import conninfo_to_dict

        given = set(conninfo_to_dict(url))
    except Exception:                                        # noqa: BLE001 — 읽지 못하는 URL 은 connect 가 알린다
        given = set()
    want = {**KEEPALIVE, **({"tcp_user_timeout": user_timeout_ms} if user_timeout_ms else {})}
    extra = {k: v for k, v in want.items() if k not in given}
    return psycopg.connect(url, connect_timeout=max(1, round(timeout_s)), autocommit=False, **extra)


connect = psycopg_connect      # 연결은 이 이름 하나를 거친다 — 시험이 바꿔 끼운다 (부를 때 찾는다)


def _connect_fn(fn: Callable | None) -> Callable:
    return fn if fn is not None else globals()["connect"]


# ── 우리 쪽의 기다림의 상한 (tasks/0009 4.1 라) ─────────────────────────────────────────
# 멈춰 버린 서버: 연결은 살아 있는데 서버의 프로세스가 돌지 않으면(가상 머신의 일시 정지, 저장 장치의 멈춤 — 실험에서는 그 백엔드에 SIGSTOP)
# 서버가 거는 시간 제한도, TCP keepalive 도 작동하지 않는다 (서버의 커널이 답한다). 풀릴 때까지 기다렸다 (153초까지 보고 풀었다) — 그동안
# 작업 스레드(접수·처리)가 선다. 그래서 문장 하나(execute·COPY·커밋)마다 statement_timeout_s + 30초 안에 돌아오지 않으면
# ① cancel_safe(timeout=5) — 취소도 새 연결이라 멈춘 서버에서는 기다린다 (그래서 시간 제한을 주고, 다른 스레드에서 그만큼만 기다린다 —
#    libpq 17 보다 옛 판에서는 cancel_safe 가 시간 제한 없는 cancel 로 돌아간다)
# ② 그래도 돌아오지 않으면 그 연결의 소켓에 shutdown() — 다른 스레드에서 close()·PQfinish 를 부르지 않는다 (libpq 가 쓰는 중이다).
#    기다리던 쪽이 끝을 읽고 드라이버가 예외(OperationalError, SQLSTATE 없음)를 낸다 → 종류 connection_lost, 지금의 실패 경로.
# 서버가 살아 있으면 서버의 statement_timeout 이 30초 먼저 끊는다 — 이 타이머는 서버가 답하지 않을 때만 쏜다.
WATCH_MARGIN_S = 30.0
CANCEL_TIMEOUT_S = 5.0
CANCEL_GRACE_S = 2.0              # 취소가 들었으면(서버가 살아 있다) 문장이 돌아오기를 이만큼 더 기다린 뒤에 소켓을 끊는다


class Watchdog:
    """연결 하나의 감시 타이머 (스레드 하나). `with dog():` 가 문장 하나 — 그 안에서 limit_s 를 넘기면 취소 → 소켓 끊기.
    fired: 쏜 단계 (None | "cancel" | "shutdown") — 시험과 기록용."""

    def __init__(self, conn, limit_s: float, cancel_timeout_s: float = CANCEL_TIMEOUT_S, grace_s: float = CANCEL_GRACE_S):
        self.conn, self.limit_s = conn, float(limit_s)
        self.cancel_timeout_s, self.grace_s = cancel_timeout_s, grace_s
        self.fired: str | None = None
        self._cv = threading.Condition()
        self._gen = 0                       # 문장의 번호
        self._active: int | None = None     # 지금 도는 문장의 번호 (없으면 None)
        self._deadline: float | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="publish-watchdog", daemon=True)
        self._thread.start()

    @contextlib.contextmanager
    def __call__(self):
        with self._cv:
            self._gen += 1
            self._active, self._deadline = self._gen, time.monotonic() + self.limit_s
            self._cv.notify_all()
        try:
            yield
        finally:
            with self._cv:
                self._active = self._deadline = None
                self._cv.notify_all()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        self._thread.join(timeout=self.cancel_timeout_s + self.grace_s + 1)

    def _run(self) -> None:
        with self._cv:
            while not self._closed:
                if self._deadline is None:
                    self._cv.wait()
                    continue
                left = self._deadline - time.monotonic()
                if left > 0:
                    self._cv.wait(left)
                    continue
                gen, self._deadline = self._active, None
                self._cv.release()
                try:
                    self._fire(gen)
                finally:
                    self._cv.acquire()

    def _still(self, gen) -> bool:
        return self._active is not None and self._active == gen

    def _fire(self, gen) -> None:
        self.fired = "cancel"
        done = threading.Event()
        ok: list[bool] = []

        def cancel():
            try:
                fn = getattr(self.conn, "cancel_safe", None)
                if fn is not None:
                    fn(timeout=self.cancel_timeout_s)
                    ok.append(True)
            except Exception:                                # noqa: BLE001 — CancellationTimeout·연결 실패: 소켓을 끊는다
                pass
            finally:
                done.set()

        threading.Thread(target=cancel, name="publish-cancel", daemon=True).start()
        done.wait(self.cancel_timeout_s)
        with self._cv:
            if ok:                                           # 취소가 서버에 닿았다 — 문장이 곧 돌아온다
                end = time.monotonic() + self.grace_s
                while self._still(gen) and not self._closed and time.monotonic() < end:
                    self._cv.wait(end - time.monotonic())
            if not self._still(gen):
                return
            self.fired = "shutdown"                          # 잠금 안에서 — 그 사이에 돌아온 문장 뒤의 다음 문장을 끊지 않게
            shutdown_socket(self.conn)


def shutdown_socket(conn) -> None:
    """연결의 소켓에 shutdown() 만 (닫지 않는다 — 기술자는 libpq 의 것이다). 기다리던 쪽이 끝을 읽는다."""
    try:
        fd = conn.pgconn.socket
    except Exception:                                        # noqa: BLE001 — 이미 끊겼다
        return
    try:
        s = socket.socket(fileno=fd)                         # 같은 기술자를 빌린다 (가족·형은 기술자에서)
    except OSError:
        return
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    finally:
        s.detach()                                           # 닫지 않고 돌려준다


class _Watched:
    """커서 하나: execute·executemany·copy 가 문장 하나씩 감시 타이머 안에서."""

    def __init__(self, cur, dog: Watchdog):
        self._cur, self._dog = cur, dog

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *a):
        return self._cur.__exit__(*a)

    def execute(self, *a, **kw):
        with self._dog():
            return self._cur.execute(*a, **kw)

    def executemany(self, *a, **kw):
        with self._dog():
            return self._cur.executemany(*a, **kw)

    def __getattr__(self, name):
        attr = getattr(self._cur, name)
        if name != "copy":
            return attr

        @contextlib.contextmanager
        def copy(*a, **kw):                                   # COPY 하나(보내기 + 끝의 답)가 문장 하나
            with self._dog(), attr(*a, **kw) as cp:
                yield cp

        return copy


# ── 대상 ───────────────────────────────────────────────────────────────────
class Target:
    """대상의 스키마 하나 (연결 하나). 이 클래스만 대상에 SQL 을 보낸다. watchdog: 문장마다의 감시 타이머 (없으면 감시하지 않는다 —
    가짜 연결의 시험)."""

    def __init__(self, conn, schema: str, watchdog: Watchdog | None = None):
        self.conn, self.schema = conn, ddl.check_schema_name(schema)
        self.s = q(self.schema)
        self.watchdog = watchdog

    def cur(self):
        c = self.conn.cursor()
        return c if self.watchdog is None else _Watched(c, self.watchdog)

    def _watch(self):
        return self.watchdog() if self.watchdog is not None else contextlib.nullcontext()

    def commit(self) -> None:
        with self._watch():
            self.conn.commit()

    def rollback(self) -> None:
        with self._watch():
            self.conn.rollback()

    def close(self) -> None:
        if self.watchdog is not None:
            self.watchdog.close()
        self.conn.close()

    def limit(self, lock_s: float, statement_s: float) -> None:
        """이 트랜잭션의 잠금·문장 시간 제한 (set_config(…, true) = SET LOCAL — 트랜잭션이 끝나면 풀린다: 되돌린 뒤 다시 건다.
        연결의 options 가 아니라 트랜잭션마다 거는 것은 URL 의 options 를 덮지 않고, 연결을 나눠 쓰는 풀러에서도 지켜지게)."""
        with self.cur() as c:
            c.execute("SELECT set_config('lock_timeout', %s, true), set_config('statement_timeout', %s, true)",
                      (f"{max(1, round(lock_s * 1000))}ms", f"{max(1, round(statement_s * 1000))}ms"))

    def exists(self) -> bool:
        with self.cur() as c:
            c.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                      (self.schema, META_TABLE))
            return c.fetchone()[0] > 0

    def versions(self) -> dict[str, str]:
        with self.cur() as c:
            c.execute(f"SELECT key, value FROM {self.s}.{q(META_TABLE)}")
            return dict(c.fetchall())

    def same_name_tables(self) -> int:
        """이 프로그램의 표와 이름이 같은 표의 수 (pub_meta 가 없을 때 — 이 프로그램이 만든 것이 아니다)."""
        with self.cur() as c:
            c.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = ANY(%s)",
                      (self.schema, ddl.owned_tables()))
            return c.fetchone()[0]

    def create(self, site: str) -> None:
        """표를 만든다. 스키마는 없을 때만 만든다 — CREATE SCHEMA 는 (IF NOT EXISTS 여도) DB 의 CREATE 권한이 있어야 하므로, 관리자가
        스키마를 만들어 준 전용 계정은 그 스키마 안에만 쓴다. pub_meta 에 스키마 버전·싣기의 판·사이트 이름."""
        with self.cur() as c:
            c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (self.schema,))
            if c.fetchone() is None:
                c.execute(f"CREATE SCHEMA {self.s}")
            for stmt in ddl.create_statements(self.schema):
                c.execute(stmt)
            c.executemany(f"INSERT INTO {self.s}.{q(META_TABLE)} (key, value) VALUES (%s, %s)",
                          [("schema_version", str(SCHEMA_VERSION)), ("publish_version", str(PUBLISH_VERSION)), ("site", site)])

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

    def months(self) -> set[str]:
        """대상에 있는 달 — 상태 표의 날짜 키와 대상의 쪽 날짜 가운데 달의 꼴인 것 (대상에만 있는 달도 조각이 된다, 4.2 가)."""
        with self.cur() as c:
            c.execute(f"SELECT DISTINCT substr(key, 1, 7) FROM {self.s}.{q(STATE_TABLE)} WHERE kind = 'date' AND "
                      f"{_MONTHED_PG.format(c='key')} UNION "
                      f"SELECT DISTINCT substr(work_date, 1, 7) FROM {self.s}.\"doc_page\" WHERE {_MONTHED_PG.format(c='work_date')}")
            return {r[0] for r in c.fetchall() if r[0]}

    def dates_in(self, lo: str, hi: str) -> set[str]:
        """대상에서 lo ≤ 날짜 < hi 인 날짜 키와 쪽 날짜 — 바이트 순서로 (COLLATE "C": DB 의 정렬 규칙과 무관하게 작업 DB 와 같은 비교)."""
        with self.cur() as c:
            c.execute(f"SELECT key FROM {self.s}.{q(STATE_TABLE)} WHERE kind = 'date' AND key COLLATE \"C\" >= %s AND "
                      f"key COLLATE \"C\" < %s UNION "
                      f"SELECT DISTINCT work_date FROM {self.s}.\"doc_page\" WHERE work_date COLLATE \"C\" >= %s AND "
                      f"work_date COLLATE \"C\" < %s", (lo, hi, lo, hi))
            return {r[0] for r in c.fetchall() if r[0]}

    def rest_keys(self) -> tuple[set[str], set[str]]:
        """날짜 없는 조각의 대상 쪽: 상태 표의 문서 키 가운데 대상에 달의 꼴인 쪽 날짜가 없는 것(대상에만 있는 문서 키도 — 작업 DB 에
        있는지는 견줄 때 본다), 달의 꼴이 아닌 날짜 키."""
        with self.cur() as c:
            c.execute(f"SELECT key FROM {self.s}.{q(STATE_TABLE)} s WHERE kind = 'document' AND NOT EXISTS "
                      f"(SELECT 1 FROM {self.s}.\"doc_page\" p WHERE p.document_id = s.key AND {_MONTHED_PG.format(c='p.work_date')})")
            docs = {r[0] for r in c.fetchall()}
            c.execute(f"SELECT key FROM {self.s}.{q(STATE_TABLE)} WHERE kind = 'date' AND NOT {_MONTHED_PG.format(c='key')}")
            return docs, {r[0] for r in c.fetchall()}

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


REST = "-"                                               # 날짜 없는 조각 (tasks/0009 4.2 가)
_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}\Z")
# 달의 꼴인 날짜 = 앞의 여덟 글자가 'YYYY-MM-' (ASCII 숫자). 달의 조각은 그 앞 글자로, 날짜 없는 조각은 그 꼴이 아닌 것 전부 — 둘이 꼭
# 맞물린다 ('YYYY-MM' 처럼 끝이 잘린 키도 날짜 없는 조각으로 간다). 대상(PostgreSQL)에서는 비교에 COLLATE "C" — DB 의 정렬 규칙이
# en_US·ko_KR·ICU 면 '-'·'.' 같은 문장 부호를 먼저 무시해 범위가 틀린다.
_MONTHED_SQLITE = "{c} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-*'"
_MONTHED_PG = "{c} ~ '^[0-9]{{4}}-[0-9]{{2}}-'"


def _month_range(m: str) -> tuple[str, str]:
    """앞 글자가 'YYYY-MM-' 인 글자 전부 = 'YYYY-MM-' ≤ 글자 < 'YYYY-MM.' (바이트 순서 — '.' 는 '-' 바로 다음 글자. 뒤에 무엇이 오든)."""
    return f"{m}-", f"{m}."


def slices(con, target: Target) -> list[str]:
    """한 바퀴의 조각 (tasks/0009 4.2 가): 작업 DB 의 달(쪽의 날짜·날짜 범위의 키)과 대상에만 있는 달(상태 표의 날짜 키·대상의 쪽 날짜)을
    합친 달들, 그리고 마지막에 날짜 없는 조각. 읽는 것은 달의 목록뿐이다."""
    w = _MONTHED_SQLITE.format(c="work_date")
    months = {r[0] for r in con.execute(f"SELECT DISTINCT substr(work_date, 1, 7) FROM doc_page WHERE {w}")}
    for t, c in DATE_TABLES.items():
        months |= {r[0] for r in con.execute(f"SELECT DISTINCT substr({c}, 1, 7) FROM {t} WHERE {_MONTHED_SQLITE.format(c=c)}")}
    months |= target.months()
    return sorted(m for m in months if m and _MONTH.match(m)) + [REST]


def slice_keys(con, target: Target, s: str) -> tuple[set[str], set[str]]:
    """조각 하나의 (문서, 날짜). 달: 앞 글자가 그 달인 날짜 전부(작업 DB 의 쪽·날짜 범위, 대상의 상태 표·쪽) — 그 날짜에 쪽이 있는 문서는
    plan 이 양쪽에서 더한다. 날짜 없는 조각: 달의 꼴인 날짜에 쪽이 없는 문서(작업 DB·대상 — 대상에만 있는 문서 키도), 달의 꼴이 아닌 날짜
    키 — 통째 범위는 plan 이 늘 더한다. 조각을 한 바퀴 다 돌면 전체 훑기의 키와 같다 (불변식 시험)."""
    if s != REST:
        lo, hi = _month_range(s)
        dates = {r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date >= ? AND work_date < ?",
                                           (lo, hi))}
        for t, c in DATE_TABLES.items():
            dates |= {r[0] for r in con.execute(f"SELECT DISTINCT {c} FROM {t} WHERE {c} >= ? AND {c} < ?", (lo, hi))}
        return set(), dates | target.dates_in(lo, hi)
    w = _MONTHED_SQLITE.format(c="work_date")
    docs = {r[0] for r in con.execute(
        f"SELECT document_id FROM doc_document WHERE document_id NOT IN (SELECT document_id FROM doc_page WHERE {w})")}
    odd = {r[0] for r in con.execute(f"SELECT DISTINCT work_date FROM doc_page WHERE work_date IS NOT NULL AND NOT {w}")}
    for t, c in DATE_TABLES.items():
        odd |= {r[0] for r in con.execute(f"SELECT DISTINCT {c} FROM {t} WHERE {c} IS NOT NULL AND NOT "
                                          f"{_MONTHED_SQLITE.format(c=c)}")}
    t_docs, t_odd = target.rest_keys()
    return docs | t_docs, odd | t_odd


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
            check: bool = False, now: Callable[[], str] = _now, fail_after: int | None = None, *, site: str,
            part: str | None = None, cycle: bool = False) -> Result:
    """한 번의 싣기 (한 트랜잭션). check=True 면 쓰지 않고(상태 표도) 다른 범위의 수만 Result 에.
    작업 DB 는 한 읽기 트랜잭션으로 본다 (store.db.read_txn). fail_after: 시험용 — 그 수의 범위를 넣은 뒤 예외 (한 트랜잭션인지).
    site: 싣는 쪽의 사이트 이름 — 대상의 pub_meta 의 site 와 다르면 싣지 않는다 (PublishError other_site, check 도).
    part: 조각 하나(달 'YYYY-MM' 또는 REST)를 더러운 범위에 더한다. cycle=True 면 조각의 목록을 내어(Result.slices) 첫 조각을 한다
    (tasks/0009 4.2 가 — 바퀴 끝의 싣기가 전체 훑기를 바퀴마다 한 조각씩 한다).
    메모리 (4.2 나): 지문은 범위 하나씩 내고 바뀐 범위의 목록에는 (종류, 키, 지문)만 둔다 — 넣을 때 그 범위의 행을 다시 읽는다
    (한 번에 메모리에 있는 행은 문서 하나와 날짜 범위 몇 개 몫이다. 처음 싣기는 행을 두 번 읽는다)."""
    res = Result(full=full)
    with read_txn(con):
        bad = orphans(con)
        if bad:
            raise Orphans(bad)
        if not target.exists():
            if target.same_name_tables():
                raise PublishError(NOT_OURS, 2, "not_ours")
            if check:
                keys = all_keys(con)
                res.replaced = {k: len(keys[k]) for k in KINDS}
                return res
            target.create(site)
            res.created = True                           # 바퀴 끝의 싣기는 새 조각 바퀴를 시작한다 (AutoPublish — 4.2 가)
        else:
            v = target.versions()
            if v.get("schema_version") != str(SCHEMA_VERSION):
                raise NeedRebuild("스키마 버전")
            if v.get("publish_version") != str(PUBLISH_VERSION):
                raise NeedRebuild("싣기의 판")
            if v.get("site") != site:
                raise PublishError(OTHER_SITE, 2, "other_site")
        documents, dates = set(documents), set(dates)
        if cycle:
            res.slices = slices(con, target)
            part = res.slices[0]
        if part is not None and not full:
            d, ds = slice_keys(con, target, part)
            documents |= d
            dates |= ds
        keys = plan(con, target, full, documents, dates)
        local = all_keys(con) if full else {k: local_keys(con, k, keys.get(k, ())) for k in KINDS}
        todo: list[tuple[str, str, str]] = []            # (종류, 키, 지문) — 행은 넣을 때 다시 읽는다
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
                    todo.append((sc.kind, sc.key, fp))
        if check:
            for kind, _key, _fp in todo:
                res.replaced[kind] += 1
            for kind, _key in gone:
                res.removed[kind] += 1
            return res
        # 지울 것을 먼저 다 지우고 넣는다 — 같은 키의 행이 범위 사이를 옮겨 다닌다 (점검의 이기는 쪽, 일보의 자리)
        for kind, key in gone:
            target.delete_scope(kind, key)
            res.removed[kind] += 1
        for kind, key, _fp in todo:
            target.delete_scope(kind, key)
        at = now()
        states = []
        for n, (kind, key, fp) in enumerate(todo):
            if fail_after is not None and n >= fail_after:
                raise RuntimeError("시험: 싣는 가운데의 실패")
            sc = next(build(con, kind, [key]))           # 같은 읽기 트랜잭션 — 지문을 낸 행과 같다
            if sc.unsuitable():
                res.skipped += 1                         # 옛 행은 위에서 지웠다. 상태 표에도 없다 — 다음에도 "다르다"
                res.skipped_ids.add(sc.id)
                continue
            for t, rows in sc.rows.items():
                target.insert(t, _cols(con, t), rows)
                res.rows += len(rows)
            del sc
            states.append((kind, key, fp))
            res.replaced[kind] += 1
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
    fn = _connect_fn(connect)
    try:
        if fn is psycopg_connect:                             # 시험이 바꿔 끼운 연결 함수는 (URL, 초) 만 받는다
            conn = fn(settings.publish_url, settings.publish_connect_timeout_s, user_timeout_ms(settings))
        else:
            conn = fn(settings.publish_url, settings.publish_connect_timeout_s)
    except PublishError:
        raise
    except Exception as e:                                  # noqa: BLE001 — 연결 실패의 종류만
        raise PublishError(f"통합 DB({describe_url(settings.publish_url)})에 닿지 못했습니다 ({type(e).__name__})", 2,
                           "connect") from None
    return Target(conn, settings.publish_schema, Watchdog(conn, _limits(settings)[1] + WATCH_MARGIN_S))


def _sqlstate(e: BaseException):
    return getattr(e, "sqlstate", None) or getattr(getattr(e, "diag", None), "sqlstate", None)


def _is_conflict(e: BaseException) -> bool:
    """키가 부딪쳤다 (더러운 범위만 실었는데 빠뜨린 범위가 그 키를 쥐고 있었다) — psycopg 의 IntegrityError (SQLSTATE 23…)."""
    state = _sqlstate(e)
    return type(e).__name__ in ("IntegrityError", "UniqueViolation") or (isinstance(state, str) and state.startswith("23"))


def _limits(settings) -> tuple[float, float]:
    from ..config import Settings

    s = settings if settings is not None else Settings()
    return s.publish_lock_timeout_s, s.publish_statement_timeout_s


def _failure(e: BaseException, settings) -> PublishError:
    """대상에서 난 예외 → PublishError (종류와 수만 — 드라이버의 글은 싣지 않는다). 시간 제한은 종류를 따로 둔다 (홈·요약에 보인다)."""
    lock_s, stmt_s = _limits(settings)
    state, name = _sqlstate(e), type(e).__name__
    if state == "55P03" or name == "LockNotAvailable":
        return PublishError(f"대상의 잠금을 {lock_s:g}초 넘게 기다려 이번 싣기를 그만뒀습니다 — 다른 연결이 대상의 표를 쥐고 있습니다 "
                            f"(커밋하지 않은 DB 도구, 표를 고치는 작업 …). 대상은 싣기 전 그대로이고 {AGAIN}", 2, "lock_timeout")
    if state == "57014" or name == "QueryCanceled":
        return PublishError(f"대상에서 문장 하나가 {stmt_s:g}초를 넘어(또는 관리자가 취소해) 이번 싣기를 그만뒀습니다 — "
                            f"대상은 싣기 전 그대로이고 {AGAIN}", 2, "statement_timeout")
    if name == "OperationalError" and not state:              # 연결한 뒤에 끊겼다 (keepalive·tcp_user_timeout, 서버가 꺼졌다 …)
        return PublishError(f"싣는 가운데 대상과의 연결이 끊겼습니다 — 커밋하지 못한 것은 서버가 되돌리고, {AGAIN}", 2,
                            "connection_lost")
    return PublishError(f"통합 DB 에 싣지 못했습니다 ({name}) — 대상은 싣기 전 그대로입니다", 2, name)


def run(con, settings, full: bool = True, documents: Iterable[str] = (), dates: Iterable[str] = (), check: bool = False,
        connect: Callable | None = None, target: Target | None = None, fail_after: int | None = None,
        rebuild: bool = False, site: str | None = None, part: str | None = None, cycle: bool = False) -> Result:
    """연결 → 한 트랜잭션으로 싣기 → 커밋. 더러운 범위만 실은 것이 키 충돌로 실패하면 되돌리고 같은 자리에서 전체 훑기로 다시 한다.
    실패하면 되돌리고 PublishError (종류와 수만). check=True 면 쓰지 않는다 (되돌린다). rebuild=True 면 이 프로그램의 표를 지우고
    다시 만든 뒤 싣는다 — 같은 트랜잭션이라 읽는 쪽은 빈 표를 보지 않고, 실패하면 대상은 그 전 그대로다.
    site: 사이트 이름 (없으면 사이트 팩의 [site] name — 그것도 없으면 연결하기 전에 PublishError no_site_name).
    part·cycle: 조각 (publish 를 본다 — 바퀴 끝의 싣기만 쓴다). 키 충돌로 넘어간 전체 훑기에는 조각이 없다 (전부다)."""
    name = site_name(settings, site)
    own = target is None
    t = target or open_target(settings, connect)
    try:
        t.limit(*_limits(settings))                          # 트랜잭션마다 (되돌리면 풀린다)
        if rebuild:
            _drop_and_create(t, settings, name)
        try:
            res = publish(con, t, full=full, documents=documents, dates=dates, check=check, fail_after=fail_after, site=name,
                          part=part, cycle=cycle)
        except Exception as e:
            _rollback(t)
            if full or check or rebuild or not _is_conflict(e):
                raise
            t.limit(*_limits(settings))
            res = publish(con, t, full=True, check=False, fail_after=fail_after, site=name)
            res.fell_back = True
        if check:
            _rollback(t)
        else:
            t.commit()
        return res
    except (PublishError, Orphans):
        _rollback(t)
        raise
    except Exception as e:                                   # noqa: BLE001 — 드라이버의 글을 싣지 않는다
        _rollback(t)
        raise _failure(e, settings) from None
    finally:
        if own:
            try:
                t.close()
            except Exception:                                # noqa: BLE001
                pass


def _drop_and_create(t: Target, settings, site: str) -> None:
    """이 프로그램이 만든 표만 지우고 다시 만든다 (CASCADE 하지 않는다 — 부른 쪽의 트랜잭션 안에서, 커밋하지 않는다).
    pub_meta 가 있을 때만 지운다 (사이트가 달라도 — 바꿔 싣는 길이다). pub_meta 가 없는데 이름이 같은 표가 있으면 지우지 않는다
    (PublishError not_ours — 이 프로그램이 만든 표가 아니다). 둘 다 없으면 그냥 만든다.
    뷰가 걸려 있어 지우지 못하면 PublishError (종류 depends). 표를 읽는 연결이 있으면 DROP 이 잠금을 기다린다 — lock_timeout."""
    try:
        if t.exists():
            t.drop()
        elif t.same_name_tables():
            raise PublishError(NOT_OURS, 2, "not_ours")
        t.create(site)
    except PublishError:
        _rollback(t)
        raise
    except Exception as e:                                   # noqa: BLE001
        _rollback(t)
        if type(e).__name__ == "DependentObjectsStillExist" or _sqlstate(e) == "2BP01":
            raise PublishError("대상의 표에 다른 것(뷰 등)이 걸려 있어 지우지 않았습니다 — 그것을 먼저 치우십시오", 2,
                               "depends") from None
        f = _failure(e, settings)
        if f.kind in ("lock_timeout", "statement_timeout", "connection_lost"):
            raise f from None
        raise PublishError(f"다시 만들지 못했습니다 ({type(e).__name__})", 2, type(e).__name__) from None


def _rollback(t: Target) -> None:
    try:
        t.rollback()
    except Exception:                                        # noqa: BLE001 — 끊긴 연결
        pass
