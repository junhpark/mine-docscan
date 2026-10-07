"""처리·검수·결정이 건드린 것 (tasks/0008 4.7): 날짜, 문서, 장비. 엑셀 내보내기와 통합 DB 싣기가 다시 볼 범위를 정한다.

넉넉하게 잡는다 — 좁혀서 놓치지 않는다 (해시·지문이 같으면 쓰지 않으므로 넉넉해도 파일은 그대로다).
  dates      쪽의 날짜 (처리 전·후, 검수한 칸의 쪽)
  documents  처리한(실패한 것 포함) 문서, 검수한 칸의 문서
  removed    처리가 지운 문서 행 (같은 경로의 옛 failed 문서 — 싣기가 대상에서도 지운다)
  refs       가동 기록에 닿은 장비 (validate.usage.equipment_ref). 계기의 연속성이 다른 날짜로 번지는 유일한 길이다 —
             그 장비의 가동 기록이 있는 모든 날짜로 넓히는 것은 쓰는 쪽이 지금의 DB 를 보고 한다 (all_dates)
  everything 전부 (범위 없이 마무리한 처리 — run)
스레드 사이에는 Box 로 넘긴다 (화면 스레드의 검수 저장 → 작업 스레드의 다음 바퀴). 값·이름은 들어 있지 않다 — 장비 ref 는 장비 ID 나
적힌 이름이므로 로그에 찍지 않는다 (수만).
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field


@dataclass
class Touched:
    dates: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)
    everything: bool = False

    def add(self, other: Touched) -> Touched:
        self.dates |= {d for d in other.dates if d}
        self.documents |= other.documents
        self.removed |= other.removed
        self.refs |= {r for r in other.refs if r}
        self.everything = self.everything or other.everything
        return self

    def __bool__(self) -> bool:
        return bool(self.dates or self.documents or self.removed or self.refs or self.everything)

    def all_dates(self, con: sqlite3.Connection) -> set[str]:
        """dates + 장비(refs)의 가동 기록이 있는 모든 날짜 + 문서의 날짜와 그 문서의 쪽이 있는 날짜 (지금의 DB 에서 —
        버린 문서는 쪽이 없어도 문서의 날짜로 대기 수에 든다)."""
        from .validate.usage import equipment_ref

        out = {d for d in self.dates if d}
        if self.refs:
            for r in con.execute("SELECT work_date, equipment_id, equipment FROM eq_usage_daily WHERE work_date IS NOT NULL"):
                if equipment_ref(r) in self.refs:
                    out.add(r["work_date"])
        if self.documents:
            ids = sorted(self.documents)
            for i in range(0, len(ids), 400):           # 400 × 2 자리 < SQLite 의 옛 상한 999
                chunk = ids[i:i + 400]
                marks = ",".join("?" * len(chunk))
                out |= {r[0] for r in con.execute(
                    f"SELECT DISTINCT work_date FROM doc_page WHERE work_date IS NOT NULL AND document_id IN ({marks}) "
                    f"UNION SELECT DISTINCT work_date FROM doc_document WHERE work_date IS NOT NULL AND document_id IN ({marks})",
                    chunk + chunk)}
        return out


class Box:
    """스레드 사이의 Touched 하나 (잠금 하나). put 은 화면 스레드, take 는 작업 스레드."""

    def __init__(self):
        self._lock = threading.Lock()
        self._t = Touched()

    def put(self, t: Touched) -> None:
        with self._lock:
            self._t.add(t)

    def take(self) -> Touched:
        with self._lock:
            t, self._t = self._t, Touched()
        return t
