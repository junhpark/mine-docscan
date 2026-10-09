"""자동 내보내기 (tasks/0008 4.7): watch·serve 의 작업 스레드가 바퀴의 끝에 엑셀 폴더에 쓴다 (접수 → 처리 → 내보내기 → 싣기).

- **더러운 날짜**만 다시 본다: 이번 바퀴의 처리가 건드린 것(Pipeline.touched) + 화면 스레드의 검수 저장이 넘긴 것(mark — 작업 스레드를
  깨우지 않는다) → 날짜(장비의 가동 기록이 있는 모든 날짜와 문서의 쪽이 있는 날짜로 넓혀 — touched.Touched.all_dates) + 그 달.
  해시가 같은 파일은 쓰지 않으므로 넉넉해도 파일은 그대로다.
- **전체 훑기**: 시작할 때 한 번, 그 뒤 sweep_minutes 마다 (0 이면 시작할 때만) — 다른 프로세스가 쓴 검수(review serve)와 놓친 것을 잡는다.
  시계는 주입한다 (시험이 잠들지 않게).
- 쓰지 못한 파일(엑셀이 열고 있다)·없어진 엑셀 폴더는 그 바퀴만 건너뛰고 다음 바퀴에 다시 본다 (폴더를 만들지 않는다). 처리는 계속된다.
- excel_dir 가 없으면 꺼져 있다. 저장소 안·접수 폴더 안·보관 폴더 안이면 한 줄로 알리고(시작할 때) 켜지 않는다. 사이트 팩에
  [site] name 이 없어도 그렇다 (사본의 주인 — tasks/0009 4.1 다). 폴더가 다른 사이트의 것이면 바퀴마다 기록 파일만 보고 아무것도 하지 않는다
  (요약은 처음 한 번, 홈: "다른 사이트의 폴더").
- status 는 화면이 읽는다 (통째로 바꿔 끼우는 사전): 켜짐·꺼짐과 이유, 마지막으로 쓴 시각과 파일 수, 쓰지 못한 파일 수, 폴더가 없다.
  수만 — 날짜·파일명을 담지 않는다.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from ..touched import Box, Touched
from .writer import (
    DAILY_RE,
    MONTHLY_RE,
    NO_SITE_NAME,
    RECORD_NAME,
    ExportError,
    Result,
    check_out_dir,
    export_excel,
    now_iso,
)


class AutoExport:
    def __init__(self, settings, site, clock: Callable[[], float] = time.monotonic, now: Callable[[], str] = now_iso):
        self.site, self.clock, self.now = site, clock, now
        self.out = settings.excel_dir
        self.sweep_s = max(0.0, float(settings.export_sweep_minutes)) * 60.0
        self.machine_values = settings.machine_values
        self.box = Box()
        self.reason: str | None = None              # 꺼진 이유: off(설정 없음) | refused(저장소·접수 폴더·보관 폴더 안) | no_site_name
        self.notice: str | None = None              # 시작할 때 한 번 알릴 한 줄
        self.last_sweep: float | None = None
        self.last: Result | None = None
        self._retry_days: set[str] = set()
        self._retry_months: set[str] = set()
        if self.out is None:
            self.reason = "off"
        elif not getattr(site, "declared_name", None):
            self.reason, self.notice = "no_site_name", f"엑셀 자동 내보내기를 켜지 않습니다: {NO_SITE_NAME}"
        else:
            try:
                check_out_dir(self.out, settings)
            except ExportError as e:
                self.reason, self.notice = "refused", f"엑셀 자동 내보내기를 켜지 않습니다: {e}"
        self.status: dict = {"enabled": self.enabled, "reason": self.reason, "last_at": None, "written": 0, "deleted": 0,
                             "failed": 0, "missing_dir": False, "other_site": False, "rounds": 0}

    @property
    def enabled(self) -> bool:
        return self.reason is None

    def mark(self, t: Touched) -> None:
        """화면 스레드: 검수 저장이 건드린 것. 작업 스레드를 깨우지 않는다 — 다음 바퀴(poll_seconds 안)에 묶어서 한다."""
        if self.enabled and t:
            self.box.put(t)

    def after_round(self, con, touched: Touched) -> Result | None:
        """작업 스레드의 바퀴 끝 (부른 쪽의 연결로 — 한 읽기 트랜잭션). 돌려주는 값: 한 내보내기의 결과 (하지 않았으면 None)."""
        if not self.enabled:
            return None
        t = Touched().add(touched).add(self.box.take())
        now = self.clock()
        full = t.everything or self.last_sweep is None or (self.sweep_s > 0 and now - self.last_sweep >= self.sweep_s)
        try:
            if full:
                r = export_excel(con, self.site, self.out, full=True, machine_values=self.machine_values)
            else:
                days = t.all_dates(con) | self._retry_days
                months = {d[:7] for d in days} | self._retry_months
                if not days and not months:
                    return None
                r = export_excel(con, self.site, self.out, days=days, months=months, machine_values=self.machine_values)
        except Exception:
            self.box.put(t)                           # 다음 바퀴에 다시 (부른 쪽이 예외의 종류를 남긴다)
            raise
        self.last = r
        seen_other = self.status["other_site"]
        if r.missing_dir or r.other_site or RECORD_NAME in r.failed:
            self.box.put(t)                           # 아무것도 하지 못했다 — 다음 바퀴에 같은 범위로
        else:
            if full:
                self.last_sweep = now
            self._retry_days, self._retry_months = set(), set()
            for rel in r.failed:                      # 바꾸지 못한 파일 — 다음 바퀴에 다시
                if m := DAILY_RE.match(rel):
                    self._retry_days.add(m.group(2))
                elif m := MONTHLY_RE.match(rel):
                    self._retry_months.add(m.group(1))
        st = dict(self.status, missing_dir=r.missing_dir, other_site=r.other_site, failed=len(r.failed),
                  rounds=self.status["rounds"] + 1)
        if r.written or r.deleted:
            st.update(last_at=self.now(), written=len(r.written), deleted=len(r.deleted))
        self.status = st
        if r.other_site and seen_other:
            return None                               # 다른 사이트의 폴더 — 요약은 처음 한 번 (홈에는 늘 보인다)
        return r
