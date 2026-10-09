"""조각으로 나눈 전체 훑기 (tasks/0009 4.2 가): 자동 내보내기·자동 싣기가 전체 훑기를 한 번에 하지 않고 **바퀴마다 한 조각**씩 한다.

- 조각 = 달 하나 (그리고 싣기는 마지막에 날짜 없는 조각). 목록은 바퀴(한 번의 전체 훑기)를 시작할 때 정한다 — 그 사이에 생긴 달은 더러운
  범위가 잡고, 다음 바퀴가 훑는다.
- `sweep_minutes` 마다 한 바퀴를 시작하고, 작업 바퀴마다 한 조각씩 끝까지 간다. 시작할 때도 조각으로 돈다 (sweep_minutes = 0 이면
  시작할 때 한 바퀴만). 조각을 한 바퀴 다 돌면 지금의 전체 훑기와 같다 (불변식 시험).
- 다 돌면 마지막 전체 훑기의 시각(`last_sweep` — 시계, `last_sweep_at` — 글)을 적는다. 다음 바퀴는 그 뒤 `sweep_minutes` 에 시작한다.
- 상태(`status`)는 화면이 읽는다: 돌고 있으면 {"done", "total", "first"} ("처음 훑는 중 3/13").
시계는 주입한다 (시험이 잠들지 않게).
"""
from __future__ import annotations

from collections.abc import Callable


class Sweep:
    def __init__(self, sweep_s: float, clock: Callable[[], float], now: Callable[[], str]):
        self.sweep_s, self.clock, self.now = max(0.0, float(sweep_s)), clock, now
        self.parts: list[str] | None = None          # 남은 조각 (None — 바퀴가 돌고 있지 않다)
        self.total = 0
        self.done = 0
        self.first = True                            # 아직 한 바퀴도 다 돌지 않았다
        self.force = False                           # 다음 작업 바퀴에 새 바퀴를 시작한다 (전부 건드렸다, 대상의 표를 새로 만들었다)
        self.last_sweep: float | None = None         # 마지막으로 한 바퀴를 다 돈 때 (시계)
        self.last_sweep_at: str | None = None

    @property
    def running(self) -> bool:
        return self.parts is not None

    def due(self) -> bool:
        """새 바퀴를 시작할 때인가 (돌고 있지 않을 때만)."""
        if self.running:
            return False
        return self.force or self.last_sweep is None or (self.sweep_s > 0 and self.clock() - self.last_sweep >= self.sweep_s)

    def restart(self) -> None:
        """돌던 바퀴를 버리고 다음 작업 바퀴에 새로 시작한다."""
        self.parts, self.force = None, True

    def cancel(self) -> None:
        """이번에 시작한 바퀴를 없던 것으로 (아무것도 하지 못했다 — 폴더가 없다 …). 다음 작업 바퀴에 다시 시작한다."""
        self.parts, self.force = None, True

    def begin(self, parts: list[str]) -> None:
        self.parts, self.total, self.done, self.force = list(parts), len(parts), 0, False

    @property
    def part(self) -> str | None:
        return self.parts[0] if self.parts else None

    def advance(self) -> None:
        """이번 조각을 마쳤다. 남은 조각이 없으면 바퀴를 마친다."""
        if self.parts:
            self.parts.pop(0)
            self.done += 1
        if self.parts is not None and not self.parts:
            self.complete()

    def complete(self) -> None:
        """한 바퀴를 다 돌았다 (또는 한 번에 전부 했다 — 키 충돌로 넘어간 전체 싣기, 달이 없어 한 번에 훑은 내보내기)."""
        self.parts, self.force, self.first = None, False, False
        self.last_sweep, self.last_sweep_at = self.clock(), self.now()

    def status(self) -> dict:
        return {"sweep": ({"done": self.done, "total": self.total, "first": self.first} if self.running else None),
                "last_sweep_at": self.last_sweep_at}
