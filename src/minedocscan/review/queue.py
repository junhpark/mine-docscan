"""검수 대기열: 무엇을 어떤 순서로 보여 줄 것인가.

대기열은 "항목"의 목록이고, 항목은 셀 하나 또는 함께 봐야 하는 셀 묶음이다.

  haul-numbers  운반 숫자 셀의 표본 (정답 만들기). 기계 값은 숨긴다 — 보여 주면 그 값에 끌린다
  mismatch      교차검증 불일치 칸마다 한 항목: 일보의 주간·야간 셀과 행렬 셀(여러 장이면 전부)을 묶는다. 기계 값 숨김
  pending       검수 대기 필드 전부, 쪽 순서 (운영용). 기계 값을 보여 주고 입력창에 미리 채운다
  page-fields   쪽의 메타(차량번호·작성자)가 되는 자유 필드 중 아직 값이 없는 것. 항목 = 쪽 하나. 후보 목록을 같이 준다.
                기계가 채운 키는 나오지 않는다. audit=N 이면 기계의 상태와 상관없이 날짜별로 고르게 뽑은 쪽 N 개에서 사람·파일명의
                값이 없는 키를 (기계 값 없이) 보여 준다 — 자동 적재된 쪽의 오류를 잴 정답 (tasks/0004 4.6)
  meta-check    기계가 읽은 메타 값과 사람(라벨·검수)의 값이 다른 쪽 (날짜의 부분은 빼고). 두 값을 보여 주고 맞는 값을 입력받는다
  checks        점검표의 장비 행 표본 (✓ 판정의 정답, tasks/0004 단계 6). 항목 = 행 하나(유·무 두 칸). 기계의 판정은 숨긴다.
                판정 불가인 행, 점검을 하지 않은 날(column_unused)의 행도 모집단에 있다 — 표시가 없다는 것도 정답이다.
                검수한 행도 목록에 남긴다 (answer) — 다시 열면 전에 고른 답이 보인다

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

from .store import effective, field_id_of

QUEUES = ("haul-numbers", "mismatch", "pending", "page-fields", "meta-check", "checks")
DEFAULT_N = {"haul-numbers": 1500, "checks": 300}
HUMAN_SOURCES = ("review", "label", "filename")
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
    meta_key: str | None = None             # page-fields: 이 셀의 값이 되는 메타 키 (후보 목록의 키)
    human: dict | None = None               # meta-check: 사람·파일명의 값 {value, source}


@dataclass
class QueueItem:
    item_id: str
    title: str
    work_date: str | None
    cells: list[QueueCell] = field(default_factory=list)
    answer: str | None = None               # checks: 검수로 정해진 행의 답 (유 / 무 / 표시 없음 / 모름)


def build_queue(con: sqlite3.Connection, name: str, *, n: int | None = None, seed: int = 0, empty_share: float = 0.1,
                template: str | None = None, kind: str | None = None, site=None, audit: int | None = None) -> dict:
    candidates: dict = {}
    n = DEFAULT_N.get(name, 0) if n is None else n
    if name == "haul-numbers":
        items, total, done = _haul_numbers(con, n, seed, empty_share)
    elif name == "mismatch":
        items, total, done = _mismatch(con)
    elif name == "pending":
        items, total, done = _pending(con, template, kind)
    elif name == "page-fields":
        if site is None:
            raise ValueError("page-fields 대기열에는 사이트 팩이 필요합니다 (템플릿의 meta_key 와 라벨)")
        items, total, done, candidates = _page_fields(con, site, audit, seed)
    elif name == "meta-check":
        if site is None:
            raise ValueError("meta-check 대기열에는 사이트 팩이 필요합니다")
        items, total, done, candidates = _meta_check(con, site)
    elif name == "checks":
        if site is None:
            raise ValueError("checks 대기열에는 사이트 팩이 필요합니다 (점검표 템플릿의 유·무 칸)")
        items, total, done = _checks(con, site, n, seed)
    else:
        raise KeyError(f"알 수 없는 대기열 '{name}' (가능: {QUEUES})")
    return {"name": name, "total": total, "done": done, "items": [asdict(i) for i in items], "candidates": candidates}


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


# ── page-fields ────────────────────────────────────────────────────────────
def _page_fields(con, site, audit: int | None = None, seed: int = 0):
    """meta_key 필드가 있는 양식의 쪽마다, 쪽 메타(doc_page_meta 의 최종 값: 검수값 > 라벨 > 파일명 > 기계 값)에 아직 없는
    키의 필드를 한 항목으로 묶는다. 기계가 채운 키는 나오지 않는다. 날짜의 부분(date.month·date.day)은 검수로 받지 않는다.
    audit=N: 날짜별로 고르게(hash(seed, page_id) 순서로 날짜를 돌아가며) 뽑은 쪽 N 개에서 사람·파일명의 값이 없는 키 — 기계가
    채웠든 아니든. 표본은 씨앗으로 고정되고, 검수가 진행되어도 구성이 바뀌지 않는다. 기계 값은 보여 주지 않는다 (4.6)."""
    metas = {name: t.review_meta_fields() for name, t in site.templates.items() if t.review_meta_fields()}
    if not metas:
        return [], 0, 0, {}
    pages = con.execute(
        "SELECT p.page_id, p.page_no, p.work_date, p.template_name, d.source_name FROM doc_page p "
        f"JOIN doc_document d ON p.document_id = d.document_id WHERE p.status = 'loaded' AND p.template_name IN "
        f"({','.join('?' * len(metas))}) ORDER BY p.work_date, d.source_name, p.page_no", list(metas)).fetchall()
    if audit:
        pages = sorted(_stratified_pages(pages, audit, seed), key=lambda r: (r["work_date"] or "", r["source_name"],
                                                                              r["page_no"]))
    items, done = [], 0
    for pg in pages:
        meta = _human_meta(con, pg["page_id"]) if audit else _final_meta(con, pg["page_id"])
        missing = {name: key for name, key in metas[pg["template_name"]].items() if not meta.get(key)}
        if not missing:
            done += 1
            continue
        cells = []
        for name, key in missing.items():
            r = con.execute(_FIELD_SQL + "WHERE f.field_id = ?", (field_id_of(pg["page_id"], name),)).fetchone()
            if r is not None:
                cells.append(QueueCell(r["field_id"], key, r["kind"], None, None, meta_key=key))
        if cells:
            title = f"{pg['work_date'] or '날짜 없음'} · {pg['template_name']} · {pg['source_name']}#{pg['page_no']}"
            items.append(QueueItem(pg["page_id"], title, pg["work_date"], cells))
    keys = sorted({k for m in metas.values() for k in m.values()})
    return items, len(pages), done, {k: _candidates(con, site, k) for k in keys}


def _final_meta(con, page_id: str) -> dict:
    return {r[0]: r[1] for r in con.execute("SELECT meta_key, value FROM doc_page_meta WHERE page_id = ? "
                                            "AND value IS NOT NULL", (page_id,))}


def _human_meta(con, page_id: str) -> dict:
    """사람·파일명에서 온 값만 (기계가 채운 값은 빠진다)."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT meta_key, value FROM doc_page_meta WHERE page_id = ? AND value IS NOT NULL "
        f"AND source IN ({','.join('?' * len(HUMAN_SOURCES))})", (page_id, *HUMAN_SOURCES))}


def _stratified_pages(pages, n: int, seed: int) -> list:
    """날짜마다 hash(seed, page_id) 순서로 줄을 세우고 날짜를 돌아가며 하나씩 — 기계의 상태와 상관없이."""
    by_date: dict[str, list] = {}
    for pg in pages:
        by_date.setdefault(pg["work_date"] or "", []).append(pg)
    queues = [sorted(v, key=lambda r: _rank(seed, r["page_id"])) for _k, v in sorted(by_date.items())]
    out: list = []
    while len(out) < n and any(queues):
        for q in queues:
            if q and len(out) < n:
                out.append(q.pop(0))
    return out


def _meta_check(con, site):
    """기계 값과 사람 값이 다른 (쪽, 키) — 날짜의 부분은 빼고 (날짜는 검수로 받지 않는다). 항목 = 쪽 하나, 셀마다 두 값.
    이미 검수한 키(출처 review)는 끝난 것으로 센다. 입력한 값이 검수가 된다 — 기계 값이 맞았으면 그 값을, 라벨이 맞았으면 라벨 값을."""
    rows = con.execute(
        "SELECT m.page_id, m.meta_key, m.value, m.source, m.field_id, m.machine_value, m.machine_confidence, m.machine_status, "
        "p.page_no, p.work_date, p.template_name, d.source_name FROM doc_page_meta m JOIN doc_page p ON m.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id WHERE m.check_result = 'mismatch' AND m.field_id IS NOT NULL "
        "AND m.meta_key NOT LIKE 'date%' ORDER BY p.work_date, d.source_name, p.page_no, m.meta_key").fetchall()
    by_page: dict[str, list] = {}
    for r in rows:
        by_page.setdefault(r["page_id"], []).append(r)
    items, done = [], 0
    for pid, rs in by_page.items():
        todo = [r for r in rs if r["source"] != "review"]
        done += len(rs) - len(todo)
        if not todo:
            continue
        cells = []
        for r in todo:
            f = con.execute(_FIELD_SQL + "WHERE f.field_id = ?", (r["field_id"],)).fetchone()
            if f is None:
                continue
            cells.append(QueueCell(r["field_id"], f"{r['meta_key']} — 라벨과 기계가 다르다", f["kind"], None,
                                   {"has_value": f["has_value_raw"], "value_raw": r["machine_value"],
                                    "confidence": r["machine_confidence"], "backend": f["backend"],
                                    "status": r["machine_status"]},
                                   meta_key=r["meta_key"], human={"value": r["value"], "source": r["source"]}))
        if cells:
            r0 = rs[0]
            items.append(QueueItem(f"meta-check:{pid}", f"{r0['work_date'] or '날짜 없음'} · {r0['template_name']} · "
                                   f"{r0['source_name']}#{r0['page_no']}", r0["work_date"], cells))
    keys = sorted({r["meta_key"] for r in rows})
    return items, len(rows), done, {k: _candidates(con, site, k) for k in keys}


# ── checks ─────────────────────────────────────────────────────────────────
def _checks(con, site, n: int, seed: int):
    """점검표의 장비 행을 날짜별로 고르게 n 개 (hash(seed, item_id) 순서로 날짜를 돌아가며). 기계의 상태와 상관없이 뽑는다 —
    판정 불가·column_unused 행도. 셀에는 기계 값을 싣지 않는다 (machine=None). 검수한 행은 answer 와 함께 남는다."""
    from .checks import answers, check_rows

    rows = check_rows(con, site)
    by_date: dict[str, list] = {}
    for c in rows:
        by_date.setdefault(c.work_date or "", []).append(c)
    queues = [sorted(v, key=lambda c: _rank(seed, c.item_id)) for _k, v in sorted(by_date.items())]
    sample: list = []
    while len(sample) < n and any(queues):
        for q in queues:
            if q and len(sample) < n:
                sample.append(q.pop(0))
    sample.sort(key=lambda c: (c.work_date or "", c.source_name, c.page_no, c.row_no))
    done = answers(con, sample)
    eff = effective(con, field_ids=[f for c in sample for f in (c.yes["field_id"], c.no["field_id"])])
    items = []
    for c in sample:
        cells = [QueueCell(f["field_id"], label, "checkmark", _review_dict(eff.get(f["field_id"])))
                 for f, label in ((c.yes, "유"), (c.no, "무"))]
        title = f"{c.work_date or '날짜 없음'} · {c.template_name} · {c.row_key} · {c.source_name}#{c.page_no}"
        items.append(QueueItem(c.item_id, title, c.work_date, cells, answer=done.get(c.item_id)))
    return items, len(sample), len(done)


def _candidates(con, site, key: str) -> list[str]:
    """키의 후보 값: 행렬 템플릿 머리글(header_<key>) + 라벨과 검수에 나온 값. 많이 나온 순."""
    counts: dict[str, int] = {}
    for t in site.templates.values():
        for reg in t.regions:
            for c in reg["columns"]:
                v = c.get(f"header_{key}")
                if v not in (None, ""):
                    counts[str(v)] = counts.get(str(v), 0) + 1
    for lab in site.labels.values():
        v = lab.get(key)
        if v not in (None, ""):
            counts[str(v)] = counts.get(str(v), 0) + 1
    names = {(t.name, name) for t in site.templates.values() for name, k in t.review_meta_fields().items() if k == key}
    for rv in effective(con).values():
        if rv.verdict == "value" and rv.region == "fields" and (rv.template, rv.field_name) in names:
            counts[rv.value] = counts.get(rv.value, 0) + 1
    return [v for v, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
