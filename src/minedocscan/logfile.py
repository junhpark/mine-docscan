"""serve --log-dir (tasks/0009 4.5 나): 표준 출력·오류를 폴더의 날짜별 파일에.

작업 스케줄러는 serve 를 pythonw 로 띄운다 — 창이 없고 표준 출력·오류가 None 이다. 그래서 무엇을 찍기 전에 이 파일을 연다.
- 파일: <폴더>/serve-YYYYMMDD.log (그 PC 의 날짜 — 시계 주입), UTF-8, 날이 바뀌면 새 파일, 줄마다 바로 쓴다(flush).
- 열 때와 날이 바뀔 때 KEEP_DAYS(30) 일보다 오래된 serve-*.log 를 지운다 — 이름의 날짜로 (수정 시각이 아니라).
- 두 스레드(작업·화면)가 같이 쓴다 — 잠금 하나.
쓰는 것은 지금의 요약과 같다 — 수와 문서 ID, 서버의 경로와 상태 코드 (값·이름·파일명을 찍지 않는 규칙은 찍는 쪽의 것이다).
"""
from __future__ import annotations

import io
import re
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

KEEP_DAYS = 30
NAME = re.compile(r"(?P<prefix>.+)-(?P<day>\d{8})\.log\Z")


class DailyLog(io.TextIOBase):
    """글자 흐름 — 날짜별 파일에 쓴다 (sys.stdout·sys.stderr 대신). 줄 단위로 쓴다: print 는 글과 줄바꿈을 따로 쓰므로 스레드마다
    줄이 끝날 때까지 모았다가 잠금 안에서 한 줄씩 (두 스레드의 줄이 섞이지 않고, 한 줄이 자정을 사이에 두고 나뉘지 않는다).
    파일에 쓰지 못하면(디스크가 찼다, 다른 프로그램이 잠갔다) 그 줄을 버리고 다음 줄에 다시 연다 — 찍다가 서버가 죽지 않게."""

    def __init__(self, folder: str | Path, prefix: str = "serve", keep_days: int = KEEP_DAYS,
                 clock: Callable[[], float] = time.time):
        self.folder, self.prefix, self.keep_days, self.clock = Path(folder), prefix, keep_days, clock
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._pending = threading.local()
        self._day: date | None = None
        self._f = None
        self.dropped = 0                                          # 쓰지 못해 버린 줄 수
        self._open()

    @property
    def encoding(self) -> str:
        return "utf-8"

    def _today(self) -> date:
        return datetime.fromtimestamp(self.clock()).date()

    def path_for(self, day: date) -> Path:
        return self.folder / f"{self.prefix}-{day:%Y%m%d}.log"

    def _open(self) -> None:
        day = self._today()
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None
        self._day = day
        self._f = open(self.path_for(day), "a", encoding="utf-8", newline="")  # 프로세스가 끝날 때까지 (close)
        self.prune()

    def prune(self) -> list[Path]:
        """KEEP_DAYS 일보다 오래된 이 접두어의 파일을 지운다 (이름의 날짜로). 돌려주는 값: 지운 파일."""
        limit = self._today() - timedelta(days=self.keep_days)
        gone = []
        for p in self.folder.glob(f"{self.prefix}-*.log"):
            m = NAME.match(p.name)
            if not m or m["prefix"] != self.prefix:
                continue
            try:
                day = datetime.strptime(m["day"], "%Y%m%d").date()
            except ValueError:
                continue
            if day < limit:
                try:
                    p.unlink()
                    gone.append(p)
                except OSError:                                   # 다른 프로그램이 열고 있다 — 다음에
                    pass
        return gone

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        buf = getattr(self._pending, "text", "") + s
        *lines, rest = buf.split("\n")
        self._pending.text = rest
        if lines:
            self._emit("".join(line + "\n" for line in lines))
        return len(s)

    def _emit(self, text: str) -> None:
        with self._lock:
            try:
                if self._f is None or self._today() != self._day:
                    self._open()
                self._f.write(text)
                self._f.flush()
            except (OSError, ValueError):                         # 쓰지 못했다 — 버리고 다음 줄에 다시 연다
                self.dropped += text.count("\n")
                try:
                    if self._f is not None:
                        self._f.close()
                except OSError:
                    pass
                self._f = None

    def flush(self) -> None:
        rest = getattr(self._pending, "text", "")
        if rest:                                                  # 줄바꿈 없이 끝난 글 (트레이스백의 마지막 줄 …)
            self._pending.text = ""
            self._emit(rest + "\n")

    def close(self) -> None:
        self.flush()
        with self._lock:
            if self._f is not None:
                try:
                    self._f.close()
                except OSError:
                    pass
                self._f = None
        super().close()


def redirect(folder: str | Path, prefix: str = "serve", clock: Callable[[], float] = time.time) -> DailyLog:
    """표준 출력·오류를 날짜별 파일로 (둘이 같은 파일에). 그 전의 흐름이 버리는 곳(utf8_streams 의 os.devnull)이면 닫는다.
    돌려주는 값: 그 흐름."""
    import os
    import sys

    log = DailyLog(folder, prefix, clock=clock)
    for name in ("stdout", "stderr"):
        old = getattr(sys, name)
        if old is not None and getattr(old, "name", None) == os.devnull:
            try:
                old.close()
            except OSError:
                pass
        setattr(sys, name, log)
    return log


def early_log_dir(argv: list[str]) -> str | None:
    """serve --log-dir DIR 이면 DIR — 인자를 읽기 전에 (인자가 틀려 argparse 가 멈춘 이유도 그 파일에 남게, pythonw)."""
    if not argv or argv[0] != "serve":
        return None
    for i, a in enumerate(argv):
        if a == "--log-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--log-dir="):
            return a.split("=", 1)[1]
    return None
