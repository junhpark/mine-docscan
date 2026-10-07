"""작업 한 바퀴 (tasks/0007 4.7·4.9): 접수 폴더를 훑어 접수(intake/inbox.py) → 대기 중인 문서를 문서의 순서대로 처리(4.8).

`watch --once` 는 한 바퀴, `watch` 와 `serve` 의 작업 스레드는 바퀴를 되풀이한다 (run_forever — 멈춤 신호와 깨우기 신호로 기다린다,
시험은 잠들지 않는다). 처리 밖에서 난 예외도 그 문서를 failed 로 남기고 다음으로 간다 — 작업은 죽지 않는다.
상태(status)는 화면이 읽는다: 통째로 바꿔 끼우는 사전 {"state": idle | processing, "document_id", "page_no", "n_pages"}.
요약과 로그에는 수와 문서 ID 만 — 파일명·이름은 doc list 와 화면에서 본다.
"""
from __future__ import annotations

import threading

KEYS = ("needs_date", "failed", "unreachable", "duplicates")


class Worker:
    def __init__(self, pipe, inbox=None):
        self.pipe, self.inbox = pipe, inbox
        self.status: dict = {"state": "idle"}
        self.rounds = 0
        self.last: dict | None = None
        self.last_error: str | None = None
        self._hooks = (pipe.on_document, pipe.on_page)
        pipe.on_document, pipe.on_page = self._on_document, self._on_page

    def _on_document(self, document_id: str) -> None:
        row = self.pipe.con.execute("SELECT n_pages FROM doc_document WHERE document_id = ?", (document_id,)).fetchone()
        self.status = {"state": "processing", "document_id": document_id, "page_no": 0,
                       "n_pages": row[0] if row is not None else None}
        if self._hooks[0]:
            self._hooks[0](document_id)

    def _on_page(self, document_id: str, page_no: int) -> None:
        self.status = {**self.status, "document_id": document_id, "page_no": page_no}
        if self._hooks[1]:
            self._hooks[1](document_id, page_no)

    def run_once(self) -> dict:
        """한 바퀴. 돌려주는 값: {"processed": n, "received"·"already"·…(접수 폴더가 있을 때), "needs_date"·"failed"·"unreachable":
        [문서 ID] (이번 바퀴에 생긴 것), "duplicates": n}."""
        s = self.pipe.summary
        before = {k: len(s[k]) for k in KEYS}
        out: dict = {}
        try:
            if self.inbox is not None:
                r = self.inbox.round(self.pipe)
                out.update({"received": r["received"], "already": r["already"], "moved_failed": r["moved_failed"],
                            "waiting": r["waiting"], "retry": r["retry"]})
                failed_at_register = r["failed"]
            else:
                failed_at_register = []
            out["processed"] = self.pipe.process_pending(catch=True)
        finally:
            self.status = {"state": "idle"}
        for k in ("needs_date", "failed", "unreachable"):
            ids = [d["document_id"] if isinstance(d, dict) else d for d in s[k][before[k]:]]
            if k == "failed":
                ids += failed_at_register
            if ids:
                out[k] = sorted(set(ids))
        n_dup = len(s["duplicates"]) - before["duplicates"]
        if n_dup:
            out["duplicates"] = n_dup
        self.rounds += 1
        self.last = out
        return out

    def run_forever(self, stop: threading.Event, poll_seconds: float, wake: threading.Event | None = None,
                    on_round=None) -> None:
        """멈춤 신호가 올 때까지 바퀴를 돈다. 바퀴 사이에는 poll_seconds 동안(또는 깨우기 신호 — 결정을 저장했을 때) 기다린다.
        바퀴 하나가 통째로 실패해도(접수 폴더가 끊겼다 …) 다음 바퀴를 돈다 — 그 예외의 종류만 남긴다."""
        while not stop.is_set():
            try:
                out = self.run_once()
                self.last_error = None
                if on_round:
                    on_round(out)
            except Exception as e:                             # noqa: BLE001 — 작업 스레드는 죽지 않는다
                self.last_error = type(e).__name__
                if self.pipe.con.in_transaction:
                    self.pipe.con.rollback()
            if wake is not None:
                wake.wait(poll_seconds)
                wake.clear()
            else:
                stop.wait(poll_seconds)


def format_round(r: dict) -> str:
    """한 바퀴의 요약 — 수와 문서 ID 만 (파일명·이름 없이)."""
    parts = [f"처리한 문서 {r.get('processed', 0)}건"]
    if r.get("received"):
        parts.append(f"받은 문서 {len(r['received'])}건 ({', '.join(r['received'])})")
    for k, label in (("already", "이미 있는 파일(_already)"), ("moved_failed", "읽을 수 없는 파일(_failed)"),
                     ("waiting", "아직 쓰이는 중인 파일"), ("retry", "옮기지 못해 다음에 다시 할 파일"),
                     ("duplicates", "다시 스캔 의심 쪽")):
        if r.get(k):
            parts.append(f"{label} {r[k]}")
    for k, label in (("needs_date", "날짜를 정할 문서"), ("failed", "실패한 문서"), ("unreachable", "원본에 닿지 않은 문서")):
        if r.get(k):
            parts.append(f"{label} {len(r[k])}건 ({', '.join(r[k])})")
    return ", ".join(parts)
