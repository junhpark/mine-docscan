"""자동 내보내기 (tasks/0008 4.7): watch·serve 의 작업 스레드가 바퀴의 끝에 엑셀 폴더에 쓴다 (접수 → 처리 → 내보내기 → 싣기).

- **더러운 날짜**만 다시 본다: 이번 바퀴의 처리가 건드린 것(Pipeline.touched) + 화면 스레드의 검수 저장이 넘긴 것(mark — 작업 스레드를
  깨우지 않는다) → 날짜(문서의 쪽이 있는 날짜, 연속성 행이 바뀐 쪽의 날짜 — touched.Touched.all_dates) + 그 달.
  해시가 같은 파일은 쓰지 않으므로 넉넉해도 파일은 그대로다.
- **전체 훑기는 조각으로** (tasks/0009 4.2 가 — sweep.Sweep): 시작할 때 한 바퀴, 그 뒤 sweep_minutes 마다 (0 이면 시작할 때만) — 다른
  프로세스가 쓴 검수(review serve)와 놓친 것을 잡는다. 한 바퀴는 달마다 한 조각이고 작업 바퀴마다 한 조각씩(그 바퀴의 더러운 날짜와 같이)
  한다. 달의 목록은 작업 DB 의 달과 기록 파일·폴더에만 있는 달 (엑셀에는 날짜 없는 파일이 없다 — 날짜 없는 조각이 없다). 기록 파일이
  없거나 깨졌으면(처음 쓰는 빈 폴더, 누가 지웠다) 그 바퀴에 전부 다시 쓰지 않고 새 조각 바퀴로 다시 쓴다 — 기록에 없는 파일은 해시를
  모르므로 조각마다 쓴다. 시계는 주입한다 (시험이 잠들지 않게).
- 쓰지 못한 파일(엑셀이 열고 있다)은 retry_seconds(60초)가 지난 뒤에 다시 한다 — 그 사이의 바퀴는 그 파일의 모델을 만들지 않는다 (4.2 라:
  바퀴마다 3초에 모델을 만들고 임시 파일을 쓰고 실패하고 한 줄을 찍었다). 바퀴의 요약은 쓰지 못한 파일의 수가 바뀔 때만 (홈에는 늘 보인다).
  없어진 엑셀 폴더는 그 바퀴만 건너뛰고 다음 바퀴에 다시 본다 (폴더를 만들지 않는다). 처리는 계속된다.
- excel_dir 가 없으면 꺼져 있다. 저장소 안·접수 폴더 안·보관 폴더 안이면 한 줄로 알리고(시작할 때) 켜지 않는다. 사이트 팩에
  [site] name 이 없어도 그렇다 (사본의 주인 — tasks/0009 4.1 다). 폴더가 다른 사이트의 것이면 바퀴마다 기록 파일만 보고 아무것도 하지 않는다
  (요약은 처음 한 번, 홈: "다른 사이트의 폴더").
- status 는 화면이 읽는다 (통째로 바꿔 끼우는 사전): 켜짐·꺼짐과 이유, 마지막으로 쓴 시각과 파일 수, 쓰지 못한 파일 수, 폴더가 없다.
  수만 — 날짜·파일명을 담지 않는다.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from ..sweep import Sweep
from ..touched import Box, Touched
from .writer import (
    DAILY_RE,
    MONTHLY_RE,
    NO_SITE_NAME,
    RECORD_NAME,
    ExportError,
    RecordUnreadable,
    Result,
    check_out_dir,
    export_excel,
    load_record,
    months_of,
    now_iso,
    odd_page_days,
)


class AutoExport:
    def __init__(self, settings, site, clock: Callable[[], float] = time.monotonic, now: Callable[[], str] = now_iso,
                 once: bool = False):
        """once: 다음 바퀴가 없다 (watch --once) — 할 때가 된 전체 훑기를 조각으로 나누지 않고 그 바퀴에 한 번에 한다."""
        self.site, self.clock, self.now, self.once = site, clock, now, once
        self.out = settings.excel_dir
        self.sweep_s = max(0.0, float(settings.export_sweep_minutes)) * 60.0
        self.retry_s = max(0.0, float(settings.export_retry_seconds))
        self.machine_values = settings.machine_values
        self.box = Box()
        self.reason: str | None = None              # 꺼진 이유: off(설정 없음) | refused(저장소·접수 폴더·보관 폴더 안) | no_site_name
        self.notice: str | None = None              # 시작할 때 한 번 알릴 한 줄
        self.sweep = Sweep(self.sweep_s, clock, now)
        self.last: Result | None = None
        self._failed: dict[str, float] = {}         # 바꾸지 못한 파일 → 실패한 때 (retry_seconds 뒤에 다시)
        self._record_failed = False                   # 기록 파일을 읽거나 쓰지 못했다 (다음 바퀴에 다시 — 기다리지 않는다)
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
                             "failed": 0, "missing_dir": False, "other_site": False, "rounds": 0, **self.sweep.status()}

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
        """화면 스레드: 검수 저장이 건드린 것. 작업 스레드를 깨우지 않는다 — 다음 바퀴(poll_seconds 안)에 묶어서 한다."""
        if self.enabled and t:
            self.box.put(t)

    def _failing(self) -> int:
        """쓰지 못하고 있는 파일의 수 (기록 파일 포함) — 홈과 요약이 같은 수를 쓴다."""
        return len(self._failed) + int(self._record_failed)

    def after_round(self, con, touched: Touched) -> Result | None:
        """작업 스레드의 바퀴 끝 (부른 쪽의 연결로 — 한 읽기 트랜잭션). 돌려주는 값: 한 내보내기의 결과 (하지 않았으면 None)."""
        if not self.enabled:
            return None
        t = Touched().add(touched).add(self.box.take())
        if t.everything:                               # 전부 건드렸다 (run, 장비 마스터) — 새 조각 바퀴로
            self.sweep.restart()
            t.everything = False
        now = self.clock()
        start = self.sweep.due()
        try:                                           # 무엇이 실패하든 건드린 것은 다음 바퀴로 (부른 쪽이 예외의 종류를 남긴다)
            if start:
                is_dir = self.out.is_dir()
                try:
                    record = load_record(self.out)[0] if is_dir else {}
                except RecordUnreadable:
                    record = {}
                self.sweep.begin(months_of(con, self.out, record) if is_dir else [])
                odd = odd_page_days(con)                 # ISO 가 아닌 쪽 날짜 — 바퀴마다 한 번 알린다 (어느 조각에도 들지 않는 것까지)
            part = self.sweep.part
            empty_cycle = start and (part is None or self.once)   # 달이 없다 (빈 DB·빈 폴더) 또는 다음 바퀴가 없다 — 한 번 전부 훑고 마친다
            if empty_cycle:
                part = None
            waiting = {rel for rel, at in self._failed.items() if now - at < self.retry_s}
            due = set(self._failed) - waiting
            dirty = t.all_dates(con)
            days = dirty | {m.group(2) for rel in due if (m := DAILY_RE.match(rel))}
            months = {d[:7] for d in dirty} | {m.group(1) for rel in due if (m := MONTHLY_RE.match(rel))}
            if not days and not months and part is None and not empty_cycle:
                return None
            r = export_excel(con, self.site, self.out, days=days, months=months, full=empty_cycle,
                             machine_values=self.machine_values, slices=[part] if part else (), skip=waiting, full_if_lost=False)
        except Exception:
            self.box.put(t)                           # 조각은 그대로 — 다음 바퀴에 같은 조각
            if start:
                self.sweep.cancel()                   # 이번에 시작한 바퀴는 없던 것으로 (빈 목록으로 멈춰 서지 않게)
            raise
        if start:
            r.skipped_dates = max(r.skipped_dates, odd)
        self.last = r
        seen_other = self.status["other_site"]
        before = self._failing()
        self._record_failed = RECORD_NAME in r.failed
        if r.missing_dir or r.other_site or self._record_failed:
            self.box.put(t)                           # 아무것도 하지 못했다 — 다음 바퀴에 같은 범위로 (조각도 그대로)
            if start:
                self.sweep.cancel()                   # 이번에 시작한 바퀴는 없던 것으로 — 다음 바퀴에 달의 목록부터 다시
        else:
            if empty_cycle:
                self.sweep.complete()                 # 한 번에 전부 훑었다 — 한 바퀴를 마친 것
            elif r.record_lost and not start:
                self.sweep.restart()                  # 도는 사이에 기록을 잃었다 — 앞의 조각의 해시도 없다: 새 조각 바퀴로 전부 다시 쓴다
            elif part is not None:
                self.sweep.advance()                  # (바퀴를 시작할 때 잃었으면 — 빈 폴더 — 이 바퀴가 전부 다시 쓴다)
            failed = set(r.failed)
            self._failed = {rel: at for rel, at in self._failed.items() if rel in waiting and rel not in failed}
            self._failed.update({rel: now for rel in failed})     # 바꾸지 못한 파일 — retry_seconds 뒤에 다시
        r.failing = self._failing()
        r.failed_changed = r.failing != before
        st = dict(self.status, missing_dir=r.missing_dir, other_site=r.other_site, failed=r.failing,
                  rounds=self.status["rounds"] + 1, **self.sweep.status())
        if r.written or r.deleted:
            st.update(last_at=self.now(), written=len(r.written), deleted=len(r.deleted))
        self.status = st
        if r.other_site and seen_other:
            return None                               # 다른 사이트의 폴더 — 요약은 처음 한 번 (홈에는 늘 보인다)
        return r
