"""파이프라인은 한 번에 하나만 돈다 (tasks/0007 4.1) — run, watch, serve 의 작업 스레드.

WORK_ROOT 의 작은 SQLite 파일(pipeline.lock)에 배타 트랜잭션을 잡고 있는다. 프로세스가 죽으면 운영체제가 파일 잠금을 풀므로
죽은 프로세스의 잠금이 남지 않는다 (표준 라이브러리만으로 윈도우에서도 된다). 잠금은 명령이 잡는다 — Pipeline 객체가 아니다
(시험이 한 프로세스에서 Pipeline 을 여럿 만드는 것은 그대로 된다). `doc …` 명령과 화면은 결정을 남기기만 하므로 잠금과 상관없다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

LOCK_NAME = "pipeline.lock"


class PipelineBusy(RuntimeError):
    """다른 파이프라인이 이미 돌고 있다 (한 줄)."""


class PipelineLock:
    def __init__(self, work_root: str | Path):
        self.path = Path(work_root) / LOCK_NAME
        self._con: sqlite3.Connection | None = None

    @classmethod
    def for_settings(cls, settings) -> PipelineLock:
        """그 설정의 DB 하나에 잠금 하나: SQLite 면 DB 파일 옆(기본은 WORK_ROOT), 아니면 WORK_ROOT — 작업 폴더가 달라도 DB 가
        같으면 같은 잠금이다."""
        url = settings.resolved_db_url
        return cls(Path(url[len("sqlite:///"):]).parent if url.startswith("sqlite:///") else settings.work_root)

    def acquire(self) -> PipelineLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=0, isolation_level=None, check_same_thread=False)
        try:
            con.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            con.close()
            raise PipelineBusy(f"다른 파이프라인(run·watch·serve)이 이미 돌고 있습니다 — 작업 폴더 하나에 하나만: {self.path.parent}") from None
        self._con = con
        return self

    def release(self) -> None:
        if self._con is not None:
            try:
                self._con.rollback()
            finally:
                self._con.close()
                self._con = None

    @property
    def held(self) -> bool:
        return self._con is not None

    def __enter__(self) -> PipelineLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
