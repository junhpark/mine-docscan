"""문서·쪽의 결정 기록 (tasks/0007 4.3) — 검수 기록(ADR 0008, review/store.py)과 같은 방식.

원본은 검수 파일과 같은 폴더의 추가 전용 파일 `decisions.jsonl` 이다 (기본 `<site>/reviews/decisions.jsonl`, MINEDOCSCAN_REVIEWS 를
주면 그 옆 — Settings.decisions_path). 한 줄 = 결정 하나. 지우거나 덮어쓰지 않는다. DB 의 doc_decision 은 그 사본이다.

  종류     대상          뜻
  date     문서·쪽       날짜를 정한다 (value 는 ISO 날짜)
  discard  문서·쪽       버린다 (걸린 스캔, 다시 스캔한 쪽)
  restore  문서·쪽       버린 것을 되살린다
  keep     쪽            다시 스캔한 것이 아니다 — 다른 종이다 (4.6)

유효한 결정 = 대상마다, 묶음(date / discard·restore / keep)마다 파일에서 가장 뒤의 것 하나. 버린 쪽에 keep 이 있어도 버린 것이다.
결정은 업무 테이블을 직접 고치지 않는다: 저장 = 파일에 한 줄 → doc_decision → 그 문서의 work_requested +1. 그 뒤 처리
(Pipeline.process_document)가 그 문서의 행을 지우고 결정을 적용해 다시 만든다 — 화면·명령·`--fresh` 가 같은 길을 지난다.
결정 기록에는 값·이름이 없다 (note 는 사람이 적은 메모 — 로그에 찍지 않는다).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path

from ..store.db import upsert, write_txn
from .dates import DateError, iso_date, parse_date, warning

KINDS = ("date", "discard", "restore", "keep")
DOCUMENT_KINDS = ("date", "discard", "restore")         # 문서에 거는 결정 (keep 은 쪽만)
GROUP = {"date": "date", "discard": "discard", "restore": "discard", "keep": "keep"}
_PAGE = re.compile(r"^(?P<doc>.+)-p(?P<n>[0-9]+)$")       # 쪽 ID = document_id-pN (다시 처리해도 같다)


class DecisionError(ValueError):
    """결정을 저장할 수 없다 (한 줄 — 무엇이 왜). 이때는 파일에도 DB 에도 아무것도 남기지 않는다."""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_target(target: str) -> tuple[str, int | None]:
    """대상 → (document_id, 쪽 번호 | None). 문서 ID 는 해시(16자리 16진수)라 "-p<숫자>" 로 끝나지 않는다."""
    m = _PAGE.match(target or "")
    return (m["doc"], int(m["n"])) if m else (target, None)


@dataclass
class Decision:
    target: str
    kind: str
    value: str = ""
    decided_by: str = ""
    decided_at: str = ""                  # ISO 8601 UTC, 초 단위. 비우면 지금
    note: str = ""
    decision_id: str = ""                 # 비우면 내용에서 만든다

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise DecisionError(f"알 수 없는 결정 '{self.kind}' (가능: {', '.join(KINDS)})")
        if not self.target or not self.decided_by:
            raise DecisionError("대상과 결정한 사람(--reviewer)은 비울 수 없습니다")
        if self.kind == "date":
            if iso_date(self.value) is None:
                raise DecisionError(f"날짜 결정의 값은 ISO 날짜여야 합니다: {self.value!r}")
        else:
            self.value = ""
        if self.page_no is None and self.kind not in DOCUMENT_KINDS:
            raise DecisionError(f"'{self.kind}' 는 쪽에만 거는 결정입니다 (쪽 ID: <문서 ID>-p<쪽>)")
        if not self.decided_at:
            self.decided_at = now_iso()
        if not self.decision_id:
            key = "|".join((self.target, self.kind, self.value, self.decided_by, self.decided_at, self.note))
            self.decision_id = hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def document_id(self) -> str:
        return split_target(self.target)[0]

    @property
    def page_no(self) -> int | None:
        return split_target(self.target)[1]

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps({"decision_id": d.pop("decision_id"), **d}, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> Decision:
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def db_row(self, seq: int) -> dict:
        return {"decision_id": self.decision_id, "seq": seq, "target": self.target, "document_id": self.document_id,
                "page_no": self.page_no, "kind": self.kind, "value": self.value or None, "decided_by": self.decided_by,
                "decided_at": self.decided_at, "note": self.note or None}


# ── 파일 ───────────────────────────────────────────────────────────────────
def append(path: str | Path, decisions: list[Decision]) -> list[int]:
    """파일 끝에 줄을 붙이고 flush 한다 (한 번에 — 여러 결정이 한 저장). 돌려주는 값은 줄 번호(1부터) = seq."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    first, prefix = 1, ""
    if path.exists():
        data = path.read_bytes()
        first = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1) + 1
        prefix = "" if (not data or data.endswith(b"\n")) else "\n"      # 쓰다 끊긴 줄 뒤에는 줄을 바꿔서 쓴다
    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + "".join(d.to_json() + "\n" for d in decisions))
        f.flush()
    return list(range(first, first + len(decisions)))


def load(path: str | Path) -> tuple[list[tuple[int, Decision]], int]:
    """파일 전체를 (seq, Decision) 목록으로. 깨진 줄(쓰다 끊긴 마지막 줄 등)은 건너뛰고 그 수를 같이 돌려준다."""
    path = Path(path)
    if not path.exists():
        return [], 0
    out, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for seq, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append((seq, Decision.from_dict(json.loads(line))))
            except (ValueError, TypeError, KeyError):
                skipped += 1
    return out, skipped


def import_into(con: sqlite3.Connection, path: str | Path) -> dict:
    """파일 → doc_decision. 파일을 그대로 비춘다 (지우고 다시 읽는다) — 다른 파일에서 읽었던 행이 남아 "가장 뒤의 결정"을
    바꾸지 않게. 대상이 DB 에 없어도 된다 (문서가 등록되면 붙는다). 파일은 쓰는 트랜잭션 안에서 읽는다 — 그 사이에 다른 연결이
    저장한 결정(파일에 붙이고 DB 에 쓴다)을 지우지 않게. 유효한 결정이 바뀐 문서(다른 컴퓨터·백업에서 온 줄)에는 다시 처리를
    요청한다 — 처리된 문서가 옛 결정으로 남지 않게 (4.8 의 불변식)."""
    with write_txn(con):
        decisions, skipped = load(path)
        before = _effective_all(con)
        con.execute("DELETE FROM doc_decision")
        upsert(con, "doc_decision", [d.db_row(seq) for seq, d in decisions])
        after = _effective_all(con)
        changed = sorted(d for d in before.keys() | after.keys() if before.get(d) != after.get(d))
        if changed:
            request_work(con, changed)
    return {"path": str(path), "imported": len(decisions), "skipped": skipped}


def _effective_all(con: sqlite3.Connection) -> dict[str, DocDecisions]:
    return {d: effective(con, d) for (d,) in con.execute("SELECT DISTINCT document_id FROM doc_decision")}


# ── 유효한 결정 ────────────────────────────────────────────────────────────
@dataclass
class TargetDecisions:
    date: str | None = None               # 정한 날짜 (ISO)
    discarded: bool = False
    keep: bool = False                    # 쪽: 다시 스캔한 것이 아니다


@dataclass
class DocDecisions:
    doc: TargetDecisions = field(default_factory=TargetDecisions)
    pages: dict[int, TargetDecisions] = field(default_factory=dict)

    def page(self, page_no: int) -> TargetDecisions:
        return self.pages.get(page_no) or TargetDecisions()

    def page_date(self, page_no: int) -> str | None:
        """쪽의 날짜를 정한 결정: 쪽의 결정 > 문서의 결정."""
        return self.page(page_no).date or self.doc.date

    @property
    def empty(self) -> bool:
        return self.doc == TargetDecisions() and not self.pages


def effective(con: sqlite3.Connection, document_id: str) -> DocDecisions:
    """그 문서와 쪽들의 유효한 결정 (묶음마다 파일에서 가장 뒤의 것)."""
    out = DocDecisions()
    for r in con.execute("SELECT * FROM doc_decision WHERE document_id = ? ORDER BY seq", (document_id,)):
        t = out.doc if r["page_no"] is None else out.pages.setdefault(r["page_no"], TargetDecisions())
        if r["kind"] == "date":
            t.date = r["value"]
        elif r["kind"] in ("discard", "restore"):
            t.discarded = r["kind"] == "discard"
        elif r["kind"] == "keep":
            t.keep = True
    return out


def decided_date(con: sqlite3.Connection, page_id: str) -> str | None:
    """그 쪽의 날짜를 정한 결정 (쪽의 결정 > 문서의 결정). 없으면 None. 쪽 메타를 다시 계산할 때 (pagemeta.refresh_page)."""
    doc, page_no = split_target(page_id)
    if page_no is None:
        return None
    return effective(con, doc).page_date(page_no)


# ── 저장 ───────────────────────────────────────────────────────────────────
def received_day(row, tz: tzinfo | None = None) -> date:
    """그 문서를 받은 날 — 해가 없는 날짜 표기의 해와 되묻기의 기준. 받은 시각(UTC)을 그 컴퓨터의 날짜로 바꾼다 (tz=None 이면
    현장 PC 의 시간대): 한국 시각 03-27 08:30 에 받은 문서는 UTC 로 03-26 이다. 받은 시각이 없으면 오늘."""
    v = row["received_at"] if row is not None else None
    if v:
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(tz).date()
        except ValueError:
            pass
    return datetime.now(UTC).astimezone(tz).date()


def normalize_target(target: str) -> str:
    """대상의 표기를 하나로: 문서 ID 는 소문자, 쪽 번호는 앞의 0 없이 (docid-p01 → docid-p1 — 쪽 ID 와 같게)."""
    doc, page_no = split_target(str(target or "").strip())
    doc = doc.lower()
    return doc if page_no is None else f"{doc}-p{page_no}"


def save(con: sqlite3.Connection, path: str | Path, items: list[dict], reviewer: str,
         received: date | None = None, now: str | None = None) -> dict:
    """결정 여럿을 한 번에 저장한다. items: [{target, kind, value?, note?}]. 전부 검사한 뒤에야 쓴다 — 하나라도 틀리면
    DecisionError 이고 파일에도 DB 에도 아무것도 남지 않는다. 날짜는 intake.dates 로 읽는다 (받은 날: received, 없으면 그 문서의
    received_at). 저장 = 파일에 줄을 붙이고(먼저) → doc_decision → 그 문서들의 work_requested +1. 읽는 것부터 쓰는 트랜잭션 안에서.
    돌려주는 값: {"decisions": [Decision], "warnings": [한 줄], "documents": [다시 처리를 요청한 문서]}."""
    if not reviewer:
        raise DecisionError("결정한 사람이 없습니다 (--reviewer)")
    if not items:
        raise DecisionError("저장할 결정이 없습니다")
    with write_txn(con):
        out, warnings = [], []
        for it in items:
            if not isinstance(it, dict):
                raise DecisionError("결정은 {target, kind, value, note} 입니다")
            target, kind = normalize_target(it.get("target")), str(it.get("kind") or "").strip()
            doc_id, page_no = split_target(target)
            row = con.execute("SELECT document_id, n_pages, received_at FROM doc_document WHERE document_id = ?",
                              (doc_id,)).fetchone()
            if row is None:
                raise DecisionError(f"모르는 문서입니다: {doc_id}")
            if page_no is not None and not (row["n_pages"] and 1 <= page_no <= row["n_pages"]):
                raise DecisionError(f"그 문서에 {page_no}쪽이 없습니다 (전체 {row['n_pages'] or 0}쪽): {doc_id}")
            value = ""
            if kind == "date":
                day = received or received_day(row)
                try:
                    value = parse_date(str(it.get("value") or ""), day)
                except DateError as e:
                    raise DecisionError(str(e)) from None
                w = warning(value, day)
                if w:
                    warnings.append(f"{target}: {w}")
            d = Decision(target=target, kind=kind, value=value, decided_by=reviewer, note=str(it.get("note") or ""),
                         decided_at=now or "")
            out.append(d)
        seqs = append(path, out)                              # 파일이 원본: 먼저 쓴다
        upsert(con, "doc_decision", [d.db_row(seq) for d, seq in zip(out, seqs, strict=True)])
        docs = sorted({d.document_id for d in out})
        request_work(con, docs)
    return {"decisions": out, "warnings": warnings, "documents": docs}


def request_work(con: sqlite3.Connection, document_ids) -> int:
    """그 문서들에 다시 처리를 요청한다 (work_requested +1 — 4.1). 전용 UPDATE 로만 (upsert 로 쓰지 않는다)."""
    ids = sorted(set(document_ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        con.execute(f"UPDATE doc_document SET work_requested = work_requested + 1 WHERE document_id IN "
                    f"({','.join('?' * len(chunk))})", chunk)
    return len(ids)


def count_lines(path: str | Path) -> int:
    """결정 파일의 줄 수 (info)."""
    path = Path(path)
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())
