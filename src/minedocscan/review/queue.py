"""검수 대기열: 무엇을 어떤 순서로 보여 줄 것인가.

대기열은 "항목"의 목록이고, 항목은 셀 하나 또는 함께 봐야 하는 셀 묶음이다.

  haul-numbers  운반 숫자 셀의 표본 (정답 만들기). 기계 값은 숨긴다 — 보여 주면 그 값에 끌린다
  mismatch      교차검증 불일치 칸마다 한 항목: 일보의 주간·야간 셀과 행렬 셀(여러 장이면 전부)을 묶는다. 기계 값 숨김
  pending       검수 대기 필드 전부, 쪽 순서 (운영용). 기계 값을 보여 주고 입력창에 미리 채운다
                계기 표의 시작·종료 칸에는 readings 와 같은 ask_dotted (tasks/0006 4.8 — 미리 채운 기계 값은 정한 값이 아니다)
  page-fields   쪽의 메타(차량번호·작성자)가 되는 자유 필드 중 아직 값이 없는 것. 항목 = 쪽 하나. 후보 목록을 같이 준다.
                기계가 채운 키는 나오지 않는다. audit=N 이면 기계의 상태와 상관없이 날짜별로 고르게 뽑은 쪽 N 개에서 사람·파일명의
                값이 없는 키를 (기계 값 없이) 보여 준다 — 자동 적재된 쪽의 오류를 잴 정답 (tasks/0004 4.6)
  meta-check    기계가 읽은 메타 값과 사람(라벨·검수)의 값이 다른 쪽 (날짜의 부분은 빼고). 두 값을 보여 주고 맞는 값을 입력받는다
  checks        점검표의 장비 행 표본 (✓ 판정의 정답, tasks/0004 단계 6). 항목 = 행 하나(유·무 두 칸). 기계의 판정은 숨긴다.
                판정 불가인 행, 점검을 하지 않은 날(column_unused)의 행도 모집단에 있다 — 표시가 없다는 것도 정답이다.
                검수한 행도 목록에 남긴다 (answer) — 다시 열면 전에 고른 답이 보인다
  readings      가동 일보의 가동 시간을 정하는 칸 (tasks/0005 단계 5, tasks/0006 4.7). 항목 = 쪽 하나: 계기 칸(시작·종료·총) +
                근무 시각 칸(shifts 표의 time_range 칸, 행 순서)을 한 번에. 그중 하나라도 잉크가 있는 쪽, audit=N 이면 잉크와 상관없이
                날짜별로 고르게 뽑은 쪽 N 개. 기계 값도 앞날의 값도 싣지 않는다 (0005 4.7 — 보여 주면 따라 적는다).
                계기 표의 시작·종료 칸(reading 형식)에는 ask_dotted — 점으로 쓴 시각(08.00)을 넣으면 화면이 묻는다 (0006 4.8).
                usage-check·pending 의 계기 칸도 같다
  usage-check   가동 일보의 검산이 어긋난 것 (xcheck_usage 의 gap·overlap·mismatch). 항목 = 검산 하나: 비교한 칸들(다른 쪽이면
                두 쪽)과 지금 값, 차이. 고칠 칸만 고쳐 저장한다 — 고쳐서 맞으면 끝, 여전히 어긋나면 남는다. 아무것도 고치지 않고
                저장하면 "종이에 적힌 대로"를 확인한 것이고 끝난다 (어긋남은 xcheck_usage 와 리포트에 그대로 남는다 — ADR 0006).
                이 대기열의 검수는 note 에 검산 id 를 남긴다 (USAGE_CHECK_NOTE). 확인 = 비교한 칸 전부가 그 검산의 항목에서 저장되었고
                지금 값이 그때와 같다 — 한 칸이 두 검산에 걸쳐 있어도(종료 칸) 둘 다 끝낼 수 있다

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
from dataclasses import asdict, dataclass, field, replace

from ..pagemeta import HUMAN_SOURCES  # 사람의 출처 목록은 한 곳 (tasks/0007 4.2)
from .store import effective, field_id_of

QUEUES = ("haul-numbers", "mismatch", "pending", "page-fields", "meta-check", "checks", "readings", "usage-check")
USAGE_CHECK_NOTE = "usage-check:"           # usage-check 대기열에서 저장한 검수의 note 앞부분 (뒤는 검산 id)
BAD_RESULTS = ("gap", "overlap", "mismatch")
METER_LABELS = {"start": "계기 시작", "end": "계기 종료", "total": "총"}
DEFAULT_N = {"haul-numbers": 1500, "checks": 300}
INPUT_KINDS = ("handwritten_number", "handwritten_text")      # 이 화면이 입력받는 셀 종류

_FIELD_SQL = (
    "SELECT f.field_id, f.page_id, f.region, f.row_no, f.field_name, f.kind, f.format, f.row_key, f.x0, f.y0, f.x1, f.y1, "
    "f.has_value_raw, f.value_raw, f.confidence, f.backend, f.review_status, f.has_value, f.value_final, "
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
    format: str | None = None               # 값의 형식 (forms/formats.py) — 화면이 받는 글자와 안내를 바꾼다
    current: dict | None = None             # usage-check: 지금의 최종 값 {value, has_value} — 고칠 칸을 고른다
    ask_dotted: bool = False                # 계기 표의 시작·종료 칸(reading): 점으로 쓴 시각을 넣으면 화면이 묻는다 (tasks/0006 4.8)


@dataclass
class QueueItem:
    item_id: str
    title: str
    work_date: str | None
    cells: list[QueueCell] = field(default_factory=list)
    answer: str | None = None               # checks: 검수로 정해진 행의 답 (유 / 무 / 표시 없음 / 모름)
    check: dict | None = None               # usage-check: 검산 {kind, result, value_a, value_b, diff, days_between}


def build_queue(con: sqlite3.Connection, name: str, *, n: int | None = None, seed: int = 0, empty_share: float = 0.1,
                template: str | None = None, kind: str | None = None, site=None, audit: int | None = None) -> dict:
    candidates: dict = {}
    n = DEFAULT_N.get(name, 0) if n is None else n
    if name == "haul-numbers":
        items, total, done = _haul_numbers(con, n, seed, empty_share)
    elif name == "mismatch":
        items, total, done = _mismatch(con)
    elif name == "pending":
        items, total, done = _pending(con, template, kind, site)
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
    elif name == "readings":
        if site is None:
            raise ValueError("readings 대기열에는 사이트 팩이 필요합니다 (가동 일보 템플릿의 계기 표)")
        items, total, done = _readings(con, site, audit, seed)
    elif name == "usage-check":
        if site is None:
            raise ValueError("usage-check 대기열에는 사이트 팩이 필요합니다")
        items, total, done = _usage_check(con, site)
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
    return QueueCell(r["field_id"], label, r["kind"], _review_dict(rv), _machine_dict(r) if show_machine else None,
                     format=r["format"])


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
def _pending(con, template: str | None, kind: str | None, site=None):
    """검수 대기 칸 하나씩 (기계 값을 미리 채운다). 사이트 팩이 있으면 계기 표의 시작·종료 칸에 ask_dotted — 같은
    handwritten_number 라 계기 칸도 여기 나온다 (tasks/0006 4.7·4.8: 묻는 것은 칸에 붙는다, 대기열이 아니라)."""
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
                       [replace(_cell(r, r["field_name"], None, show_machine=True),
                                ask_dotted=site is not None and ask_dotted(site, r))]) for r in todo]
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
        missing = {name: key for name, key in metas[pg["template_name"]].items() if key not in meta}
        if not missing:
            done += 1
            continue
        cells = []
        for name, key in missing.items():
            r = con.execute(_FIELD_SQL + "WHERE f.field_id = ?", (field_id_of(pg["page_id"], name),)).fetchone()
            if r is not None:
                cells.append(QueueCell(r["field_id"], key, r["kind"], None, None, meta_key=key, format=r["format"]))
        if cells:
            title = f"{pg['work_date'] or '날짜 없음'} · {pg['template_name']} · {pg['source_name']}#{pg['page_no']}"
            items.append(QueueItem(pg["page_id"], title, pg["work_date"], cells))
    keys = sorted({k for m in metas.values() for k in m.values()})
    return items, len(pages), done, {k: _candidates(con, site, k) for k in keys}


def _final_meta(con, page_id: str) -> dict:
    """정해진 키: 값이 있거나, 검수에서 빈 칸이라고 답한 키 (값 NULL, 출처 review — 다시 묻지 않는다)."""
    return {r[0]: r[1] for r in con.execute("SELECT meta_key, value FROM doc_page_meta WHERE page_id = ? "
                                            "AND (value IS NOT NULL OR source = 'review')", (page_id,))}


def _human_meta(con, page_id: str) -> dict:
    """사람·파일명에서 온 값만 (기계가 채운 값은 빠진다). 검수에서 빈 칸이라고 답한 키도 정해진 것으로."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT meta_key, value FROM doc_page_meta WHERE page_id = ? AND (value IS NOT NULL OR source = 'review') "
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
    입력한 값이 검수가 된다 — 기계 값이 맞았으면 그 값을, 라벨이 맞았으면 라벨 값을.
    모집단 = 지금 mismatch 인 (쪽, 키) + 검수로 끝난 것 (그 필드에 유효한 검수가 있고, 라벨·파일명의 값이 기계 값과 달랐던 것 —
    기계 값을 입력하면 match 가 되어 mismatch 에서 빠지므로). 유효한 검수(읽을 수 없음 포함)가 있으면 끝난 것으로 센다."""
    from .store import effective

    rows = con.execute(
        "SELECT m.page_id, m.meta_key, m.value, m.source, m.field_id, m.machine_value, m.machine_confidence, m.machine_status, "
        "m.check_result, p.page_no, p.work_date, p.template_name, d.source_name FROM doc_page_meta m "
        "JOIN doc_page p ON m.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
        "WHERE m.field_id IS NOT NULL AND m.meta_key NOT IN ('date', 'date.month', 'date.day') "
        "AND m.machine_status IN ('auto', 'unlisted') "
        "ORDER BY p.work_date, d.source_name, p.page_no, m.meta_key").fetchall()
    reviewed = effective(con, field_ids=[r["field_id"] for r in rows])

    def disagreed(r) -> bool:
        if r["check_result"] == "mismatch":
            return True
        if r["field_id"] not in reviewed:
            return False
        before = site.page_meta(r["source_name"], r["page_no"]).get(r["meta_key"])     # 검수 전의 사람·파일명 값
        return before not in (None, "") and str(before) != r["machine_value"]

    rows = [r for r in rows if disagreed(r)]
    by_page: dict[str, list] = {}
    for r in rows:
        by_page.setdefault(r["page_id"], []).append(r)
    items, done = [], 0
    for pid, rs in by_page.items():
        todo = [r for r in rs if r["field_id"] not in reviewed]
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
                                   meta_key=r["meta_key"], human={"value": r["value"], "source": r["source"]},
                                   format=f["format"]))
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
    """키의 후보 값: 사이트 팩이 아는 값(행렬 머리글 header_<key>, 장비명이면 [equipment.aliases] 의 이름 — SitePack.known_values)
    + 라벨과 검수에 나온 값. 많이 나온 순."""
    counts: dict[str, int] = {}
    for v in site.known_values(key):
        counts[v] = counts.get(v, 0) + 1
    for lab in site.labels.values():
        v = lab.get(key)
        if v not in (None, ""):
            counts[str(v)] = counts.get(str(v), 0) + 1
    names = {(t.name, name) for t in site.templates.values() for name, k in t.review_meta_fields().items() if k == key}
    for rv in effective(con).values():
        if rv.verdict == "value" and rv.region == "fields" and (rv.template, rv.field_name) in names:
            counts[rv.value] = counts.get(rv.value, 0) + 1
    return [v for v, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ── readings ───────────────────────────────────────────────────────────────
def _readings(con, site, audit: int | None = None, seed: int = 0):
    """가동 시간을 정하는 칸 (tasks/0006 4.7). 쪽마다 한 항목: 계기 칸(role meter — 시작·종료·총) 다음에 근무 시각 칸(role shifts 의
    time_range 칸, 행 순서). 계기 표만 있는 양식도, 근무 시각 표만 있는 양식도 된다. 그 칸 중 하나라도 기계가 잉크를 본 칸
    (has_value_raw)이 있는 쪽 — audit=N 이면 잉크와 상관없이 날짜별로 고르게 N 쪽. 셀에 기계 값을 싣지 않고, 다른 쪽(앞날)의 값도
    싣지 않는다 (0005 4.7). 항목의 칸에 전부 유효한 검수가 있으면 끝난 쪽."""
    from ..forms.template import meter_slot
    from ..handlers.usage import is_shift_cell

    roles = {t.name: {r["name"]: r["role"] for r in t.regions if r.get("role") in ("meter", "shifts")}
             for t in site.templates.values() if t.handler == "usage"}
    names = [n for n, regs in roles.items() if regs]
    if not names:
        return [], 0, 0
    rows = con.execute(_FIELD_SQL + f"WHERE p.status = 'loaded' AND f.kind LIKE 'handwritten%' AND p.template_name IN "
                       f"({','.join('?' * len(names))})", names).fetchall()
    order = {"start": 0, "end": 1, "total": 2}
    shift_labels: dict = {}
    by_page: dict[str, list] = {}                # 쪽 → [(보여 주는 순서, 라벨, 행)]
    for r in rows:
        role = roles[r["template_name"]].get(r["region"])
        slot = meter_slot(r["field_name"], r["row_key"]) if role == "meter" else None
        if slot:
            by_page.setdefault(r["page_id"], []).append(((0, order[slot], 0), METER_LABELS[slot], r))
        elif is_shift_cell(role, r):
            label = _shift_label(site, r, shift_labels)
            by_page.setdefault(r["page_id"], []).append(((1, r["row_no"], r["x0"] or 0), label, r))
    if audit:
        firsts = [min(v, key=lambda x: x[0])[2] for v in by_page.values()]
        pages = [r["page_id"] for r in _stratified_pages(firsts, audit, seed)]
    else:
        pages = [pid for pid, v in by_page.items() if any(r["has_value_raw"] for _o, _l, r in v)]
    pages.sort(key=lambda pid: _order(min(by_page[pid], key=lambda x: x[0])[2]))
    reviews = effective(con, field_ids=[r["field_id"] for pid in pages for _o, _l, r in by_page[pid]])
    items, done = [], 0
    for pid in pages:
        cells = sorted(by_page[pid], key=lambda x: x[0])
        if all(r["field_id"] in reviews for _o, _l, r in cells):
            done += 1
            continue
        r0 = cells[0][2]
        what = "·".join(w for g, w in ((0, "계기"), (1, "근무 시각")) if any(o[0] == g for o, _l, _r in cells))
        title = f"{r0['work_date'] or '날짜 없음'} · {r0['template_name']} · {r0['source_name']}#{r0['page_no']} · {what}"
        items.append(QueueItem(f"readings:{pid}", title, r0["work_date"],
                               [QueueCell(r["field_id"], label, r["kind"], _review_dict(reviews.get(r["field_id"])), None,
                                          format=r["format"], ask_dotted=ask_dotted(site, r)) for _o, label, r in cells]))
    return items, len(pages), done


def _shift_label(site, r, cache: dict) -> str:
    """근무 시각 칸의 라벨: "근무 시각 " + 행에 인쇄된 근무 구분(행 메타 shift, 없으면 행 키). 표에 time_range 열이 둘 이상이면
    열 이름을 덧붙인다. 템플릿의 값이다 — 손으로 쓴 값이 아니다."""
    key = (r["template_name"], r["region"])
    if key not in cache:
        reg = site.templates[r["template_name"]].region(r["region"])
        ranges = [c for c in reg["columns"] if c.get("format") == "time_range"]
        cache[key] = ({row["row"]: row for row in reg["rows"]}, len(ranges) > 1)
    rows, many = cache[key]
    row = rows.get(r["row_no"], {})
    label = f"근무 시각 {row.get('shift') or r['row_key'] or r['row_no']}"
    return f"{label} · {r['field_name']}" if many else label


def ask_dotted(site, r) -> bool:
    """점으로 쓴 시각을 물을 칸인가 (tasks/0006 4.8): 가동 일보의 계기 표(role meter)의 시작·종료 칸이고 형식이 reading.
    총은 가동 시간(길이)이라 묻지 않고, 근무 시각 칸(time_range)·소수(decimal)·시각(time) 칸도 묻지 않는다."""
    from ..forms.template import meter_slot

    tpl = site.templates.get(r["template_name"])
    if tpl is None or tpl.handler != "usage" or r["format"] != "reading" or not str(r["kind"]).startswith("handwritten"):
        return False
    role = next((reg.get("role") for reg in tpl.regions if reg["name"] == r["region"]), None)
    return role == "meter" and meter_slot(r["field_name"], r["row_key"]) in ("start", "end")


# ── usage-check ────────────────────────────────────────────────────────────
def check_id(c) -> str:
    return f"{c['page_id']}:{c['check_kind']}:{c['item']}"


def _usage_check(con, site):
    """xcheck_usage 의 어긋난 검산(gap·overlap·mismatch) + 이 대기열에서 손댄 검산(note 에 그 id 가 남은 검수가 있는 것).
    끝난 것 = 지금 맞는 것(고쳐서 맞게 됐다) + 비교한 칸이 전부 이 대기열에서 확인된 것(종이에 적힌 대로의 어긋남).
    항목의 셀에는 지금의 최종 값을 싣는다 (기계 값이 아니다 — 계기 칸은 기계가 읽지 않는다)."""
    from ..validate.usage import check_cells

    # 이 대기열에서 저장한 검수: (칸, 검산) → 그때의 (판정, 값)들. 한 칸이 두 검산에 들어 있을 수 있다 (종료 칸 = 그 쪽의 총 검산과 다음 기록의
    # 연속성) — 다른 검산을 확인하느라 그 칸을 다시 저장해도, 값이 그대로면 앞의 확인은 살아 있다
    confirms: dict[tuple[str, str], set] = {}
    for fid, note, verdict, value in con.execute(
            "SELECT field_id, note, verdict, value FROM doc_review WHERE note LIKE ?", (USAGE_CHECK_NOTE + "%",)):
        confirms.setdefault((fid, note[len(USAGE_CHECK_NOTE):]), set()).add((verdict, value or ""))
    noted = {cid for _f, cid in confirms}
    every = con.execute(
        "SELECT x.*, p.page_no, p.template_name, d.source_name FROM xcheck_usage x JOIN doc_page p ON x.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id ORDER BY x.work_date, d.source_name, p.page_no, x.check_kind, "
        "x.item").fetchall()
    checks = [c for c in every if c["result"] in BAD_RESULTS or check_id(c) in noted]
    # 고쳐서 검산 자체가 없어진 것(예: 총 칸을 비웠다 — 총 검산은 셋 다 값이 있을 때만)도 끝난 것으로 센다
    gone = len(noted - {check_id(c) for c in every})
    cells_of = {check_id(c): check_cells(con, site, c) for c in checks}
    reviews = effective(con, field_ids=sorted({f for fs in cells_of.values() for f in fs}))
    items, done = [], 0
    names = {"total": "총 = 종료 − 시작", "subtotal": "소계 = 합", "continuity": "계기의 연속성"}
    for c in checks:
        cid, fids = check_id(c), cells_of[check_id(c)]
        # 확인됨: 비교한 칸 전부가 이 검산의 항목에서 저장되었고, 지금 값(유효한 검수)이 그때와 같다
        confirmed = bool(fids) and all(f in reviews and (reviews[f].verdict, reviews[f].value or "") in confirms.get((f, cid), ())
                                       for f in fids)
        if c["result"] not in BAD_RESULTS or confirmed:
            done += 1
            continue
        cells = []
        for k, fid in enumerate(fids):
            r = con.execute(_FIELD_SQL + "WHERE f.field_id = ?", (fid,)).fetchone()
            if r is None:
                continue
            cur = {"value": r["value_final"] if r["has_value"] else "", "has_value": r["has_value"]}
            cells.append(QueueCell(fid, _check_label(c, k, r), r["kind"], _review_dict(reviews.get(fid)), None,
                                   format=r["format"], current=cur, ask_dotted=ask_dotted(site, r)))
        diff = "" if c["diff"] is None else f" · 차이 {c['diff']:+g}"
        title = f"{c['work_date'] or '날짜 없음'} · {c['template_name']} · {c['source_name']}#{c['page_no']} · " \
                f"{names.get(c['check_kind'], c['check_kind'])} {c['result']}{diff}"
        items.append(QueueItem(USAGE_CHECK_NOTE + cid, title, c["work_date"], cells,
                               check={k: c[k] for k in ("check_kind", "result", "value_a", "value_b", "diff", "days_between")}))
    return items, len(checks) + gone, done + gone


def _check_label(c, k: int, r) -> str:
    """검산 항목의 칸 이름: 어느 쪽의 어느 칸인지 (다른 쪽이면 그 쪽의 날짜·출처)."""
    where = f"{r['work_date'] or '날짜 없음'} · {r['source_name']}#{r['page_no']}"
    if c["check_kind"] == "continuity":
        return f"{'이 기록의 시작' if k == 0 else '앞 기록의 종료'} — {where}"
    if c["check_kind"] == "total":
        return METER_LABELS.get(r["field_name"], r["field_name"]) + f" — {where}"
    return f"{'소계' if k == 0 else '더한 칸'} {r['row_key']} {r['field_name']} — {where}"


def queue_progress(con: sqlite3.Connection, site) -> dict:
    """대기열마다 (기본 설정으로) 끝난 수 / 모집단 — review stats 가 쓴다."""
    out = {}
    for name in QUEUES:
        if name == "pending":                  # 남은 것만 세는 대기열 (끝난 것은 빠진다) — 끝남/모집단의 뜻이 없다
            continue
        try:
            q = build_queue(con, name, site=site)
        except ValueError:
            continue
        out[name] = {"done": q["done"], "total": q["total"]}
    return out
