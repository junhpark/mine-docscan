"""바퀴 끝의 싣기 (tasks/0008 4.8): watch·serve 의 작업 스레드가 엑셀 다음에 통합 DB 에 싣는다.

- **더러운 범위**만 지문을 다시 낸다: 처리·검수가 건드린 문서 + 지워진 문서 + 더러운 날짜(연속성 행이 바뀐 쪽의 날짜까지)와 그 날짜에
  쪽이 있는 문서 전부(작업 DB 와 대상 양쪽 — core.plan) + 통째(eq_equipment). 키가 부딪치면(빠뜨린 문서가 키를 쥐고 있었다) 같은 바퀴에
  전체 훑기로 다시 한다 (core.run — Result.fell_back) — 그것이 성공하면 전체 훑기를 마친 것으로 센다.
- **전체 훑기는 조각으로** (tasks/0009 4.2 가 — sweep.Sweep): 시작할 때와 sweep_minutes 마다(0 이면 시작할 때만) 한 바퀴를 시작하고,
  작업 바퀴마다 한 조각(달 하나, 마지막에 날짜 없는 조각)씩 더러운 범위와 같이 싣는다. 바퀴의 처음 조각에서 조각의 목록을 정한다
  (core.publish cycle=True). 자동 싣기의 처음 싣기도 조각으로 간다 — 다 돌 때까지 대상은 일부만 있다. 대상의 표를 새로 만든 더러운
  바퀴(Result.created)는 새 조각 바퀴를 시작한다.
- **실패하면** 그 바퀴의 싣기만 건너뛰고 건드린 것을 들고 있다가 다음에 같이 싣는다. 연결에 실패한 뒤 retry_seconds 안의 바퀴는 연결하지
  않는다 (꺼진 서버에 바퀴마다 매달리지 않게). 접수·처리·엑셀은 계속된다. 시계는 주입한다.
- psycopg 가 없으면 시작할 때 한 번 알리고 끈다. 사이트 팩에 [site] name 이 없어도 그렇다 (사본의 주인 — tasks/0009 4.1 다: 대상이 다른
  사이트의 사본이면 그 바퀴의 실패 other_site). 상태(status)에는 대상의 호스트·DB·스키마와 수, 실패의 종류만 — URL·비밀번호는 없다.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..forms.sitepack import declared_site_name
from ..sweep import Sweep
from ..touched import Box, Touched
from . import core
from .scopes import Orphans


@dataclass
class Failed:
    """그 바퀴의 싣기가 실패했다 (as_dict 는 워커의 요약으로 간다)."""
    kind: str
    message: str
    behind: int

    def as_dict(self) -> dict:
        return {"error": self.kind, "message": self.message, "behind": self.behind}


class AutoPublish:
    def __init__(self, settings, clock: Callable[[], float] = time.monotonic, now: Callable[[], str] = core._now,
                 connect: Callable | None = None, once: bool = False):
        """once: 다음 바퀴가 없다 (watch --once) — 할 때가 된 전체 훑기를 조각으로 나누지 않고 그 바퀴에 한 번에 한다 (명령 publish 처럼)."""
        self.settings, self.clock, self.now, self.connect, self.once = settings, clock, now, connect, once
        self.sweep_s = max(0.0, float(settings.publish_sweep_minutes)) * 60.0
        self.retry_s = max(0.0, float(settings.publish_retry_seconds))
        self.box = Box()
        self.held = Touched()                         # 싣지 못한 바퀴의 건드린 것 — 다음에 같이
        self.sweep = Sweep(self.sweep_s, clock, now)
        self.last_fail: float | None = None
        self.notice: str | None = None
        self.reason: str | None = None
        self.fell_back = 0                            # 더러운 범위만 싣다가 전체 훑기로 넘어간 횟수
        self.skipped: set = set()                     # 지금 대상에 없는 건너뛴 범위 — 다시 견준 바퀴에서만 고친다 (더러운 바퀴가 0 으로 덮지 않게)
        self.site = declared_site_name(settings.site)
        if not settings.publish_url:
            self.reason = "off"
        elif settings.publish_enabled is False:
            self.reason = "disabled"
        elif self.site is None:
            self.reason = "no_site_name"
            self.notice = ("사이트 팩의 site.toml 에 [site] name 이 없어 통합 DB 싣기를 끕니다 — 대상이 어느 사이트의 사본인지 적는 "
                           "이름입니다")
        elif (connect or core.connect) is core.psycopg_connect:
            try:
                import psycopg  # noqa: F401
            except ImportError:
                self.reason = "no_driver"
                self.notice = "psycopg 가 없어 통합 DB 싣기를 끕니다 — pip install -e \".[postgres]\""
        self.status: dict = {"enabled": self.enabled, "reason": self.reason,
                             "target": core.describe_url(settings.publish_url) if settings.publish_url else None,
                             "schema": settings.publish_schema, "last_ok_at": None, "behind": 0, "last_error": None,
                             "replaced": 0, "skipped": 0, "fell_back": 0, **self.sweep.status()}

    @property
    def last_sweep(self) -> float | None:
        """마지막으로 전체 훑기를 다 돈 때 (시계). 넣으면 그때 다 돈 것으로 친다 (재는 도구가 쓴다)."""
        return self.sweep.last_sweep

    @last_sweep.setter
    def last_sweep(self, t: float | None) -> None:
        self.sweep.last_sweep = t

    @property
    def enabled(self) -> bool:
        return self.reason is None

    def mark(self, t: Touched) -> None:
        """화면 스레드: 검수 저장이 건드린 것 (작업 스레드를 깨우지 않는다)."""
        if self.enabled and t:
            self.box.put(t)

    def after_round(self, con, touched: Touched):
        if not self.enabled:
            return None
        t = Touched().add(self.held).add(touched).add(self.box.take())
        if t.everything:                               # 전부 건드렸다 — 새 조각 바퀴로 (더러운 범위로 전부를 한 번에 하지 않는다)
            self.sweep.restart()
            t.everything = False
        now = self.clock()
        start = self.sweep.due()
        part = None if start else self.sweep.part
        pending = start or self.sweep.running
        if self.last_fail is not None and now - self.last_fail < self.retry_s:
            self.held = t                              # 연결하지 않는다 — 다음에
            self.status = dict(self.status, behind=_count(t, pending), **self.sweep.status())
            return None
        if not start and part is None and not t:
            return None
        whole = start and self.once                    # 다음 바퀴가 없다 — 전체 훑기를 한 번에
        try:
            res = core.run(con, self.settings, full=whole, documents=t.documents | t.removed, dates=t.all_dates(con),
                           connect=self.connect, site=self.site, part=None if whole else part, cycle=start and not whole)
        except Exception as e:                                # noqa: BLE001 — 무엇이 실패하든 건드린 것을 들고 있다가 다음에
            # 다시 하는 간격은 실패한 때부터 (시작한 때부터 재면 잠금·문장을 기다린 만큼 간격이 준다). 조각은 그대로 — 다음에 같은 조각
            self.held, self.last_fail = t, self.clock()
            kind = e.kind if isinstance(e, core.PublishError) else ("orphans" if isinstance(e, Orphans) else type(e).__name__)
            self.status = dict(self.status, behind=_count(t, pending) or 1, last_error=kind, **self.sweep.status())
            msg = str(e) if isinstance(e, core.PublishError | Orphans) else f"싣지 못했습니다 ({type(e).__name__})"
            return Failed(kind, msg, _count(t, pending) or 1)
        self.held, self.last_fail = Touched(), None
        if res.fell_back or whole:                     # 키 충돌로 넘어간 전체 싣기(한 트랜잭션)·한 번에 한 전체 훑기 — 이 바퀴를 마친 것
            self.sweep.complete()
        elif start:
            self.sweep.begin(res.slices or [])
            self.sweep.advance()                       # 첫 조각은 이번에 했다
        elif part is not None:
            self.sweep.advance()
        if res.created and not start and not res.fell_back:
            self.sweep.restart()                       # 대상의 표를 새로 만들었다 (지워졌다) — 새 조각 바퀴로 전부 싣는다
        self.fell_back += res.fell_back
        self.skipped = (self.skipped - res.checked_ids) | res.skipped_ids
        self.status = dict(self.status, last_ok_at=self.now(), behind=0, last_error=None, replaced=res.changed,
                           skipped=len(self.skipped), fell_back=self.fell_back, **self.sweep.status())
        return res


def _count(t: Touched, full: bool = False) -> int:
    """밀린 범위의 수 (문서 + 날짜). 해야 할(또는 도는 중인) 전체 훑기가 밀렸으면 그것도 하나로 센다 — 건드린 것 없이 전체 훑기가
    막혔을 때 "밀린 범위 0" 이라고 하지 않게."""
    return len(t.documents | t.removed) + len(t.dates) + (1 if t.everything or full else 0)
