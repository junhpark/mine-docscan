"""처리·검수·결정이 건드린 것 (tasks/0008 4.7): 날짜, 문서, 지운 문서. 엑셀 내보내기와 통합 DB 싣기가 다시 볼 범위를 정한다.

넉넉하게 잡는다 — 좁혀서 놓치지 않는다 (해시·지문이 같으면 쓰지 않으므로 넉넉해도 파일은 그대로다).
  dates      쪽의 날짜 (처리 전·후, 검수한 칸의 쪽), 문서의 날짜, 연속성 행이 바뀐 쪽의 날짜
  documents  처리한(실패한 것 포함) 문서, 검수한 칸의 문서, 연속성 행이 바뀐 쪽의 문서
  removed    처리가 지운 문서 행 (같은 경로의 옛 failed 문서 — 싣기가 대상에서도 지운다)
  everything 전부 (범위 없이 마무리한 처리 — run, 장비 마스터가 바뀐 처리)
계기의 연속성은 다른 날짜로 번지는 유일한 길이다 — 다시 계산한 곳(validate.usage.recompute_continuity)이 전·후로 견주어 바뀐 행의
쪽(행이 걸린 쪽과 가리키는 쪽)을 돌려주고 그 쪽의 날짜·문서를 더한다 (add_pages). 장비의 모든 날짜로 넓히지 않는다 (tasks/0009 4.2 다 —
그렇게 하면 계기 칸 하나가 84/252일을 더럽혔다). 스레드 사이에는 Box 로 넘긴다 (화면 스레드의 검수 저장 → 작업 스레드의 다음 바퀴).
값·이름은 들어 있지 않다 (날짜·ID — 로그에는 수만).
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
    everything: bool = False

    def add(self, other: Touched) -> Touched:
        self.dates |= {d for d in other.dates if d}
        self.documents |= other.documents
        self.removed |= other.removed
        self.everything = self.everything or other.everything
        return self

    def __bool__(self) -> bool:
        return bool(self.dates or self.documents or self.removed or self.everything)

    def add_pages(self, con: sqlite3.Connection, pages) -> Touched:
        """그 쪽들의 날짜와 문서를 더한다 (지금의 DB 에서 — 지워진 쪽은 그 문서를 처리한 쪽이 이미 남겼다)."""
        ids = sorted({p for p in pages or () if p})
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for doc, day in con.execute(f"SELECT document_id, work_date FROM doc_page WHERE page_id IN ({','.join('?' * len(chunk))})",
                                        chunk):
                self.documents.add(doc)
                if day:
                    self.dates.add(day)
        return self

    def all_dates(self, con: sqlite3.Connection) -> set[str]:
        """dates + 문서의 날짜와 그 문서의 쪽이 있는 날짜 (지금의 DB 에서 — 버린 문서는 쪽이 없어도 문서의 날짜로 대기 수에 든다)."""
        out = {d for d in self.dates if d}
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
