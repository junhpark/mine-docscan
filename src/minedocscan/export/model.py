"""내보내기 모델 (tasks/0008 4.2·4.3·4.6): DB → 시트들. 순수 함수 — DB 를 읽기만 하고, 시각·경로를 품지 않는다.

모델의 모양 (JSON 으로 그대로 해시한다 — 같은 DB 면 같은 모델, 같은 해시):

  책  {"model": 판, "kind": "daily" | "monthly", "key": 날짜 | 달, "sheets": [시트, …]}
  시트 {"name": 시트 이름, "rows": [[칸, …], …], "freeze": 위에서 고정할 행 수, "filter": 첫 행에 자동 필터}
  칸  [값, 표시] — 값은 글자·정수·실수·None, 표시는 아래 STYLES 의 하나 (공백으로 나눠 둘을 붙이기도 한다: "value mismatch")

엑셀 쓰기(export/xlsx.py)는 모델을 받아 파일로 옮길 뿐이다. "만든 시각"·"프로그램 판" 칸은 모델에서 값이 None 이고
(표시 stamp_time·stamp_version) 쓸 때 채운다 — 해시에 들어가지 않는다 (같은 내용을 다시 써도 해시가 같다).

칸의 상태 (4.2 — 위에서부터 먼저 맞는 줄, field_cell 한 함수가 정한다):
  printed     kind = printed                                  템플릿이 확정한 값 그대로
  meta        meta_key 가 있는 표 밖 필드이고 쪽 메타 값이 있다   그 값
  illegible   pending 이고 reviewed_by 가 있다 (검수의 illegible)  "판독 불가" — 값은 싣지 않는다
  pending     pending (그 밖)                                 "?" — 값을 적지 않는다 (기계 값이 있어도)
  empty       auto·reviewed, has_value = 0                    비운다
  mark        auto·reviewed, has_value = 1, kind = checkmark  "✓"
  value       auto·reviewed, has_value = 1, value_final 이 있다  그 값 (수 형식이면 수로)
  present     auto·reviewed, has_value = 1, value_final 이 없다  "●"
핸들러가 고쳐 말하는 곳(FormHandler.export_cells — 점검하지 않은 쪽·여백 행의 ✓ 칸, 작업 표의 글자 칸)은 그다음에 덮는다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from ..forms.template import Template, display_of
from ..handlers import get_handler
from ..intake.dates import iso_date
from ..store.order import page_key
from . import labels as L

MODEL_VERSION = 1           # 모델의 모양(시트·열·표시)을 바꾸면 올린다 — 기록 파일의 판이 다르면 다시 쓴다 (4.6)
FIELD_STATES = ("printed", "meta", "illegible", "pending", "empty", "mark", "value", "present")
UNSURE = ("pending", "illegible")          # "검수 대기 칸의 수"로 세는 상태 (4.2 — 요약·쪽의 머리·날짜별 요약이 같은 수)
STYLES = (*FIELD_STATES, "h", "title", "label", "note", "no_doc", "unknown", "mismatch", "stamp_time", "stamp_version", "")
CHUNK = 500


def cell(value=None, style: str = "") -> list:
    """칸 하나 [값, 표시]. 빈 글자는 None 으로 (엑셀은 빈 글자와 빈 칸을 가리지 않는다 — 되읽으면 같아야 한다)."""
    return [None if value == "" else value, style]


def head(*names: str) -> list[list]:
    return [cell(n, "h") for n in names]


def dict_rows(con: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    """조회의 행을 사전으로 — dict(sqlite3.Row) 와 같은 것을 더 빨리: 튜플로 받아 열 이름과 묶는다 (한 달 치 doc_field 75,000행에
    0.76 → 0.46초 — tasks/0009 4.2 마: 바뀐 것 없는 조각 하나를 5초 안에). 열 이름이 겹치는 조회(dict(Row) 는 앞의 것을 고른다)나
    연결에 다른 row_factory 가 있으면(시험이 읽은 행을 센다) 그것을 거친다."""
    if con.row_factory not in (sqlite3.Row, None):
        return [dict(r) for r in con.execute(sql, args)]
    cur = con.cursor()
    cur.row_factory = None
    try:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        if len(set(cols)) != len(cols):
            return [dict(r) for r in con.execute(sql, args)]
        return [dict(zip(cols, r, strict=True)) for r in cur]
    finally:
        cur.close()


def book_hash(book: dict) -> str:
    """내용의 해시 — 정규화한 JSON 의 SHA-256 (4.6). 파일의 바이트가 아니다 (xlsx 는 안에 만든 시각이 들어간다)."""
    return hashlib.sha256(json.dumps(book, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def is_iso_day(v) -> bool:
    """달력에 있는 ISO 날짜(YYYY-MM-DD, ASCII 숫자) 그대로인가 — 아니면 파일로 만들지 않는다 (DB 의 값이 그대로 경로가 되지 않게 — 4.3)."""
    return isinstance(v, str) and iso_date(v) == v


# ── 칸의 상태 ───────────────────────────────────────────────────────────────
def typed(value: str, fmt: str | None):
    """수 형식이면 수로 (integer → 정수, decimal·reading → 실수). 시각·범위·글자는 글자 그대로."""
    if fmt == "integer":
        try:
            return int(value)
        except ValueError:
            return value
    if fmt in ("decimal", "reading"):
        try:
            return float(value)
        except ValueError:
            return value                     # 계기 칸에 콜론으로 적은 시각 (08:00)
    return value


def field_cell(f: dict | None, meta_value: str | None = None, override: str | None = None) -> list:
    """doc_field 행 하나의 엑셀 칸 (4.2 의 첫째 표). override: 핸들러가 고쳐 말한 상태 ('empty' | 'present')."""
    if f is None:
        return cell()
    if f["kind"] == "printed":
        return cell(f["value_final"], "printed")
    if meta_value is not None:
        return cell(meta_value, "meta")
    if override == "empty":
        return cell(None, "empty")
    if override == "present":
        return cell(L.PRESENT_MARK, "present")
    if f["review_status"] == "pending":
        return cell(L.ILLEGIBLE_MARK, "illegible") if f["reviewed_by"] else cell(L.PENDING_MARK, "pending")
    if not f["has_value"]:
        return cell(None, "empty")
    if f["kind"] == "checkmark":
        return cell(L.CHECK_MARK, "mark")
    v = f["value_final"]
    if v not in (None, ""):
        return cell(typed(str(v), f["format"]), "value")
    return cell(L.PRESENT_MARK, "present")


def row_state(f: dict | None) -> str:
    """업무 행의 값이 나온 필드의 상태 (4.2 의 둘째 표): value | empty | pending | illegible. 필드가 없으면(견준 칸이 없다) value."""
    if f is None:
        return "value"
    if f["review_status"] == "pending":
        return "illegible" if f["reviewed_by"] else "pending"
    return "value" if f["has_value"] else "empty"


def worst(states) -> str:
    """여러 값의 상태 하나로: 판독 불가 > 검수 대기 > 확정 (빈 칸만이면 빈 칸)."""
    states = list(states)
    if "illegible" in states:
        return "illegible"
    if "pending" in states:
        return "pending"
    if states and all(s == "empty" for s in states):
        return "empty"
    return "value"


def sure(state: str) -> bool:
    return state not in UNSURE


# ── DB 읽기 ────────────────────────────────────────────────────────────────
@dataclass
class Pages:
    """그 날짜(들)의 쪽 — 쪽의 순서(store/order.py)대로. fields·meta 는 적재된 쪽의 것. day(d): 그 날짜의 것만 (같은 행 — 다시
    읽지 않는다. load_pages(con, [d]) 와 같다)."""
    all: list[dict]
    fields: dict[str, dict] = field(default_factory=dict)            # field_id → doc_field 행
    by_page: dict[str, dict[str, dict]] = field(default_factory=dict)  # page_id → {field_id → 행}
    meta: dict[str, dict[str, str]] = field(default_factory=dict)     # page_id → {meta_key → 값}
    meta_source: dict[str, dict[str, str]] = field(default_factory=dict)  # page_id → {meta_key → 출처}

    @property
    def loaded(self) -> list[dict]:
        return [p for p in self.all if p["status"] == "loaded"]

    def source(self, page_id: str) -> str:
        p = self.index.get(page_id)
        return f"{p['source_name']}#{p['page_no']}" if p else ""

    @property
    def index(self) -> dict[str, dict]:
        if not hasattr(self, "_index"):
            self._index = {p["page_id"]: p for p in self.all}
        return self._index

    def day(self, d: str) -> Pages:
        ps = [p for p in self.all if p["work_date"] == d]
        ids = [p["page_id"] for p in ps]
        by_page = {i: self.by_page[i] for i in ids if i in self.by_page}
        return Pages(ps, {fid: f for fs in by_page.values() for fid, f in fs.items()}, by_page,
                     {i: self.meta[i] for i in ids if i in self.meta}, {i: self.meta_source[i] for i in ids if i in self.meta_source})


PAGE_SQL = ("SELECT p.*, d.source_name, d.source_rel, d.source_path FROM doc_page p "
            "JOIN doc_document d ON p.document_id = d.document_id WHERE p.work_date IN ({})")


def load_pages(con: sqlite3.Connection, days: list[str]) -> Pages:
    rows: list[dict] = []
    for i in range(0, len(days), CHUNK):
        chunk = days[i:i + CHUNK]
        rows += dict_rows(con, PAGE_SQL.format(",".join("?" * len(chunk))), chunk)
    rows.sort(key=lambda p: (p["work_date"], page_key(p["source_rel"], p["source_path"], p["document_id"], p["page_no"])))
    pages = Pages(rows)
    ids = [p["page_id"] for p in pages.loaded]
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        marks = ",".join("?" * len(chunk))
        for d in dict_rows(con, f"SELECT * FROM doc_field WHERE page_id IN ({marks})", chunk):
            pages.fields[d["field_id"]] = d
            pages.by_page.setdefault(d["page_id"], {})[d["field_id"]] = d
        for r in con.execute(f"SELECT page_id, meta_key, value, source FROM doc_page_meta WHERE value IS NOT NULL "
                             f"AND page_id IN ({marks})", chunk):
            pages.meta.setdefault(r["page_id"], {})[r["meta_key"]] = r["value"]
            pages.meta_source.setdefault(r["page_id"], {})[r["meta_key"]] = r["source"]
    return pages


def waiting_documents(con: sqlite3.Connection, day: str) -> int:
    """처리 중이거나 다시 처리를 기다리는 문서 중 이 날짜에 걸친 것 (문서의 날짜가 이 날짜이거나 쪽이 이 날짜에 있다) — 4.3."""
    return con.execute(
        "SELECT COUNT(*) FROM doc_document d WHERE (d.status = 'received' OR d.work_requested > d.work_done) AND "
        "(d.work_date = ? OR EXISTS (SELECT 1 FROM doc_page p WHERE p.document_id = d.document_id AND p.work_date = ?))",
        (day, day)).fetchone()[0]


# ── 양식 시트 ───────────────────────────────────────────────────────────────
def family_of(site, template_name: str | None) -> str:
    tpl = site.templates.get(template_name) if template_name else None
    return tpl.sig_family if tpl is not None else (template_name or "")


def family_title(site, family: str) -> str:
    """계열의 시트 이름: 그 계열의 표시 이름 (display, 없으면 title). 계열 이름과 같은 템플릿이 있으면 그것, 아니면 이름이 가장 앞인 것
    — 동시 판은 display 가 같아야 한다 (forms/sitepack.variant_key_diff)."""
    tpls = sorted((t for t in site.templates.values() if t.sig_family == family), key=lambda t: (t.name != family, t.name))
    return tpls[0].display if tpls else family


def page_block(site, page: dict, pages: Pages) -> tuple[list[list], int]:
    """쪽 하나의 블록: 쪽의 머리 → 표 밖 필드 → 표마다 머리글 행과 그 아래 행들 (행·열의 순서는 종이와 같다).
    돌려주는 값: (행들, 검수 대기 칸의 수)."""
    pid = page["page_id"]
    fields = pages.by_page.get(pid, {})
    meta = pages.meta.get(pid, {})
    tpl: Template | None = site.templates.get(page["template_name"])
    body: list[list] = []
    notes: list[str] = []
    if tpl is None:                                     # 템플릿이 사이트 팩에서 빠졌다 — 필드를 자리 순서로만
        for f in sorted(fields.values(), key=lambda f: (f["region"], f["row_no"], f["x0"] or 0, f["field_name"])):
            body.append([cell(f["region"], "label"), cell(f["row_key"], "label"), cell(f["field_name"], "label"), field_cell(f)])
        title = page["template_name"] or ""
    else:
        try:
            handler = get_handler(tpl.handler)
        except KeyError:                                # 등록되지 않은 핸들러 — 고쳐 말하지 않는다
            handler = None
        overrides, notes = handler.export_cells(tpl, fields) if handler is not None else ({}, [])
        mk = {f["name"]: f.get("meta_key") for f in tpl.fields}
        if tpl.fields:
            body.append([cell(L.FIELDS_HEAD, "title")])
            src = pages.meta_source.get(pid, {})
            for f in tpl.fields:
                fid = f"{pid}:fields:{f['name']}:-1"
                key = mk.get(f["name"])
                row = fields.get(fid)
                mv = meta.get(key) if key else None
                if mv is not None and src.get(key) == "machine" and row is not None and row["review_status"] == "pending" \
                        and row["reviewed_by"]:
                    mv = None                           # 사람이 읽지 못한다고 한 칸 — 기계가 자동 적재한 값을 확정처럼 싣지 않는다
                body.append([cell(display_of(f, f["name"]), "label"), field_cell(row, mv, overrides.get(fid))])
        for reg in tpl.regions:
            cols = sorted(reg["columns"], key=lambda c: c["idx"])
            body.append([cell(display_of(reg, reg["name"]), "title")])
            body.append(head(L.ROW_HEAD, *(display_of(c, c["name"]) for c in cols)))
            for r in sorted(reg["rows"], key=lambda r: r["row"]):
                ids = [f"{pid}:{reg['name']}:{c['name']}:{r['row']}" for c in cols]
                body.append([cell(display_of(r, str(r.get("key", "") or "")), "label"),
                             *(field_cell(fields.get(i), None, overrides.get(i)) for i in ids)])
        title = tpl.display
    n = sum(1 for row in body for c in row if c[1] in UNSURE)
    top = [cell(L.PAGE_HEAD, "h"), cell(f"{page['source_name']}#{page['page_no']}"), cell(pid), cell(title),
           cell(L.PAGE_PENDING, "h"), cell(n)]
    return [top, *([cell(), cell(t, "note")] for t in notes), *body, []], n


def form_sheets(site, pages: Pages) -> tuple[list[dict], Counter]:
    """그 날짜에 적재된 쪽이 있는 계열마다 한 장 (계열이 처음 나온 쪽의 순서). 돌려주는 값: (시트들, 계열 → 검수 대기 칸 수)."""
    groups: dict[str, list[dict]] = {}
    for p in pages.loaded:
        groups.setdefault(family_of(site, p["template_name"]), []).append(p)
    sheets, pending = [], Counter()
    for fam, ps in groups.items():
        rows: list[list] = []
        for p in ps:
            block, n = page_block(site, p, pages)
            rows += block
            pending[fam] += n
        sheets.append({"name": family_title(site, fam), "rows": rows, "freeze": 0, "filter": False, "family": fam})
    return sheets, pending


# ── 시트 이름 ───────────────────────────────────────────────────────────────
BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\\x00-\x1f\x7f]")
RESERVED_SHEET = "history"                 # 엑셀이 쓰는 이름 (대소문자 없이)


def _clean_sheet(n: str, limit: int = 31) -> str:
    return BAD_SHEET_CHARS.sub("", str(n))[:limit].strip().strip("'").strip()


def sheet_names(names: list[str], first: set[int] | None = None) -> list[str]:
    """엑셀이 받는 시트 이름으로: 31자 안, []:*?/\\ 와 제어 문자는 뺀다, 앞뒤의 ' 는 뺀다(자른 뒤에도), 비면 '시트', 'History' 는 쓰지
    않는다. 겹치면 (2), (3) … (대소문자를 가리지 않고). first: 먼저 이름을 받는 시트의 자리 (요약·업무 시트 — 양식의 표시 이름이 그것과
    같아도 업무 시트의 이름이 바뀌지 않게)."""
    order = sorted(range(len(names)), key=lambda i: (i not in (first or set()), i))
    out: list[str] = [""] * len(names)
    seen: set[str] = set()
    for i in order:
        base = _clean_sheet(names[i]) or "시트"
        name, k = base, 1
        while name.casefold() in seen or name.casefold() == RESERVED_SHEET:
            k += 1
            suffix = f" ({k})"
            name = (_clean_sheet(base, 31 - len(suffix)) or "시트") + suffix
        seen.add(name.casefold())
        out[i] = name
    return out


def finish(kind: str, key: str, sheets: list[dict]) -> dict:
    """시트 이름을 정리하고 책을 만든다 (시트의 family 같은 안쪽 키는 뺀다). 양식 시트가 아닌 것(요약·업무 시트)이 이름을 먼저 받는다."""
    names = sheet_names([s["name"] for s in sheets], {i for i, s in enumerate(sheets) if "family" not in s})
    return {"model": MODEL_VERSION, "kind": kind, "key": key,
            "sheets": [{"name": n, "rows": s["rows"], "freeze": s.get("freeze", 0), "filter": s.get("filter", False)}
                       for n, s in zip(names, sheets, strict=True)]}
