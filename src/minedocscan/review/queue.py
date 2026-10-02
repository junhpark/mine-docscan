"""검수 대기열: 무엇을 어떤 순서로 보여 줄 것인가.

대기열은 "항목"의 목록이고, 항목은 셀 하나 또는 함께 봐야 하는 셀 묶음이다.

  haul-numbers  운반 숫자 셀의 표본 (정답 만들기). 기계 값은 숨긴다 — 보여 주면 그 값에 끌린다
  mismatch      교차검증 불일치 칸마다 한 항목: 일보의 주간·야간 셀과 행렬 셀(여러 장이면 전부)을 묶는다. 기계 값 숨김
  pending       검수 대기 필드 전부, 쪽 순서 (운영용). 기계 값을 보여 주고 입력창에 미리 채운다

haul-numbers 의 표본 규칙 (docs/tasks/0001-review-tool.md 단계 3)
  · 모집단: prod_haul 의 셀. 값이 있다고 판단된 셀(has_value_raw=1)에서 n×(1−empty_share), 비었다고 판단된 셀에서
    n×empty_share. 빈 칸 표본이 있어야 "값을 놓친" 오류를 잴 수 있다
  · 날짜 × 양식 역할(일보/행렬)로 층을 나눠 고르게 뽑는다
  · 뽑는 순서는 hash(seed, field_id). 검수가 진행되어도 표본의 구성이 바뀌지 않고 이미 한 것만 빠진다
  · 보여 주는 순서는 날짜 → 쪽 → 행 → 열

검수된 필드와 illegible 로 표시된 필드는 어느 대기열에도 다시 나오지 않는다.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass, field

from .store import effective

QUEUES = ("haul-numbers", "mismatch", "pending")
INPUT_KINDS = ("handwritten_number", "handwritten_text")      # 이 화면이 입력받는 셀 종류

_FIELD_SQL = (
    "SELECT f.field_id, f.page_id, f.region, f.row_no, f.field_name, f.kind, f.row_key, f.x0, f.y0, f.x1, f.y1, "
    "f.has_value_raw, f.value_raw, f.confidence, f.backend, f.review_status, "
    "p.template_name, p.page_no, p.work_date, d.source_name "
    "FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id ")


@dataclass
class QueueCell:
    field_id: str
    label: str                              # 열 라벨: 주간/야간, 자리 …
    kind: str
    review: dict | None = None              # 기존 유효한 검수 {verdict, value, reviewer, reviewed_at}
    machine: dict | None = None             # 기계 값 — pending 대기열에서만


@dataclass
class QueueItem:
    item_id: str
    title: str
    work_date: str | None
    cells: list[QueueCell] = field(default_factory=list)


def build_queue(con: sqlite3.Connection, name: str, *, n: int = 1500, seed: int = 0, empty_share: float = 0.1,
                template: str | None = None, kind: str | None = None) -> dict:
    if name == "haul-numbers":
        items, total, done = _haul_numbers(con, n, seed, empty_share)
    elif name == "mismatch":
        items, total, done = _mismatch(con)
    elif name == "pending":
        items, total, done = _pending(con, template, kind)
    else:
        raise KeyError(f"알 수 없는 대기열 '{name}' (가능: {QUEUES})")
    return {"name": name, "total": total, "done": done, "items": [asdict(i) for i in items]}


# ── 공통 ───────────────────────────────────────────────────────────────────
def _rank(seed: int, field_id: str) -> str:
    return hashlib.sha256(f"{seed}:{field_id}".encode()).hexdigest()


def _order(r) -> tuple:
    """보여 주는 순서: 날짜 → 문서 → 쪽 → 행 → 열(x)."""
    return (r["work_date"] or "", r["source_name"], r["page_no"], r["region"], r["row_no"], r["x0"] or 0)


def _review_dict(rv) -> dict | None:
    return None if rv is None else {"verdict": rv.verdict, "value": rv.value, "reviewer": rv.reviewer,
                                    "reviewed_at": rv.reviewed_at}


def _machine_dict(r) -> dict:
    return {"has_value": r["has_value_raw"], "value_raw": r["value_raw"], "confidence": r["confidence"],
            "backend": r["backend"]}


def _cell(r, label: str, rv, show_machine: bool) -> QueueCell:
    return QueueCell(r["field_id"], label, r["kind"], _review_dict(rv), _machine_dict(r) if show_machine else None)


def _title(r, label: str) -> str:
    return f"{r['work_date'] or '날짜 없음'} · {r['template_name']} · {r['row_key'] or r['field_name']} · {label}"


def _haul_label(h) -> str:
    """운반 셀의 열 라벨: 일보는 근무조, 행렬은 자리(와 인쇄된 차량번호)."""
    if h["source_role"] == "log":
        return h["shift"] or h["field_name"]
    return f"{h['slot']} {h['vehicle_no'] or ''}".strip()


# ── haul-numbers ───────────────────────────────────────────────────────────
def _haul_numbers(con, n: int, seed: int, empty_share: float):
    rows = con.execute(_FIELD_SQL.replace("FROM doc_field f", "FROM prod_haul h JOIN doc_field f ON h.source_field_id = f.field_id")
                       .replace("SELECT f.field_id", "SELECT h.source_role, h.shift, h.slot, h.vehicle_no, f.field_id")).fetchall()
    filled = [r for r in rows if r["has_value_raw"] == 1]
    empty = [r for r in rows if r["has_value_raw"] != 1]
    n_empty = round(n * empty_share)
    sample = _stratified(filled, n - n_empty, seed) + _stratified(empty, n_empty, seed)
    reviews = effective(con, field_ids=[r["field_id"] for r in sample])
    todo = sorted((r for r in sample if r["field_id"] not in reviews), key=_order)
    items = [QueueItem(r["field_id"], _title(r, _haul_label(r)), r["work_date"],
                       [_cell(r, _haul_label(r), None, show_machine=False)]) for r in todo]
    return items, len(sample), len(sample) - len(todo)


def _stratified(rows, quota: int, seed: int) -> list:
    """날짜 × 역할 층에서 돌아가며 하나씩 뽑는다. 층 안의 순서는 hash(seed, field_id) — 검수가 진행되어도 변하지 않는다."""
    if quota <= 0 or not rows:
        return []
    strata: dict[tuple, list] = {}
    for r in rows:
        strata.setdefault((r["work_date"] or "", r["source_role"]), []).append(r)
    queues = [sorted(v, key=lambda r: _rank(seed, r["field_id"])) for _k, v in sorted(strata.items())]
    out: list = []
    while len(out) < quota and any(queues):
        for q in queues:
            if q and len(out) < quota:
                out.append(q.pop(0))
    return out


# ── mismatch ───────────────────────────────────────────────────────────────
def _mismatch(con):
    xs = con.execute("SELECT * FROM xcheck_haul WHERE status = 'mismatch' ORDER BY work_date, slot, material, level").fetchall()
    sql = (_FIELD_SQL.replace("FROM doc_field f", "FROM prod_haul h JOIN doc_field f ON h.source_field_id = f.field_id")
           .replace("SELECT f.field_id", "SELECT h.source_role, h.shift, h.slot, h.vehicle_no, h.page_id AS haul_page, f.field_id")
           + "WHERE h.work_date = ? AND h.slot = ? AND h.material = ? AND h.level = ?")
    items, done = [], 0
    for x in xs:
        cells = con.execute(sql, (x["work_date"], x["slot"], x["material"], x["level"])).fetchall()
        cells = sorted(cells, key=lambda r: (r["source_role"] != "log", _order(r)))        # 일보 먼저, 그다음 행렬
        if not cells:
            continue
        reviews = effective(con, field_ids=[r["field_id"] for r in cells])
        if all(r["field_id"] in reviews for r in cells):
            done += 1
            continue
        qc = []
        for r in cells:
            role = "일보" if r["source_role"] == "log" else "행렬"
            qc.append(_cell(r, f"{role} {_haul_label(r)} (p{r['page_no']})", reviews.get(r["field_id"]), show_machine=False))
        title = f"{x['work_date']} · 불일치 · {x['slot']} · {x['material']}|{x['level']}"
        items.append(QueueItem(f"mismatch:{x['work_date']}:{x['slot']}:{x['material']}:{x['level']}", title, x["work_date"], qc))
    return items, len(xs), done


# ── pending ────────────────────────────────────────────────────────────────
def _pending(con, template: str | None, kind: str | None):
    kinds = [kind] if kind else list(INPUT_KINDS)
    sql = _FIELD_SQL + f"WHERE f.review_status = 'pending' AND f.kind IN ({','.join('?' * len(kinds))})"
    args: list = list(kinds)
    if template:
        sql += " AND p.template_name = ?"
        args.append(template)
    rows = con.execute(sql, args).fetchall()
    reviews = effective(con, field_ids=[r["field_id"] for r in rows])      # illegible 로 표시한 것은 다시 묻지 않는다
    todo = sorted((r for r in rows if r["field_id"] not in reviews), key=_order)
    items = [QueueItem(r["field_id"], _title(r, r["field_name"]), r["work_date"],
                       [_cell(r, r["field_name"], None, show_machine=True)]) for r in todo]
    return items, len(todo), 0
