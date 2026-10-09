"""업무 시트 (tasks/0008 4.2 의 둘째 표, 4.3): 한 행 = 업무 테이블의 한 행. 일별 파일과 월별 파일의 긴 표가 같은 함수를 쓴다.

**값 열은 그 값이 나온 필드가 확정일 때만 싣는다** — 업무 테이블의 값 열에는 검수 대기인 기계 값이 그대로 들어 있다.
확정이 아닌 값 열은 비우고 상태 열에 검수 대기(또는 판독 불가)를 적는다. 대응 (값 열 → 확정인가를 보는 곳):

  운반 prod_haul           횟수              그 행의 필드 (source_field_id — review_status, 판독 불가는 그 필드의 reviewed_by)
  작업량 prod_tally         수                그 행의 필드
  가동 기록 eq_usage_daily  계기 시작·종료·총   start/end/total_field_id 의 필드 (시각 시작·종료도 시작·종료 칸)
                          근무 시각·근무 분    그 쪽의 근무 시각 칸 전부 (handlers.usage.is_shift_cell)
                          가동 시간           근거(hours_basis)가 된 칸 전부: meter·clock → 시작·종료, total → 총·시작·종료,
                                             shifts → 근무 시각 칸 전부
  점검 insp_daily          이상 유·무          그 행의 두 ✓ 칸 (템플릿의 layout 과 장비 행에서 찾는다 — 행에는 비고 칸만 있다)
                          점검내역            source_field_id (비고 칸)
  교차검증 xcheck_haul      일보·행렬 횟수       그 날짜·자리·광종·편의 운반 행 전부 (자리 미정이면 그 쪽들의 행). 판정은 그대로,
                                             확정되지 않은 행이 끼어 있으면 "(잠정)"
  계기 검산 xcheck_usage    두 값·차이          validate.usage.check_cells 의 칸 전부. 결과는 위와 같이 "(잠정)"
  배차 eq_assignment_obs   차량번호·작성자      그대로 (쪽 메타의 최종 값)

기계 값은 기본으로 싣지 않는다 — machine_values 일 때만 "기계 값(확정 아님)" 열을 따로 두고, 확정이 아닌 행에만 기계가 읽은 값을 적는다.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from ..forms.equipment import equipment_id, is_equipment_row, layout
from ..handlers.usage import is_shift_cell
from ..store.order import page_key
from ..validate.usage import check_cells
from . import labels as L
from .model import CHUNK, Pages, cell, head, row_state, sure, worst


@dataclass
class Ctx:
    con: sqlite3.Connection
    site: object
    pages: Pages
    machine_values: bool = False
    _extra: dict = field(default_factory=dict)

    def field(self, fid: str | None) -> dict | None:
        """필드 행 — 그 날짜(들)의 적재된 쪽에 없으면(연속성 검산의 앞 기록) DB 에서."""
        if fid is None:
            return None
        if fid in self.pages.fields:
            return self.pages.fields[fid]
        if fid not in self._extra:
            r = self.con.execute("SELECT * FROM doc_field WHERE field_id = ?", (fid,)).fetchone()
            self._extra[fid] = None if r is None else dict(r)
        return self._extra[fid]

    def state(self, fid: str | None) -> str:
        """그 필드의 상태 (row_state). 필드 ID 가 없으면 견준 것이 없는 것(value — 확정을 따지지 않는다). ID 가 있는데 행이 없으면
        (템플릿을 고친 뒤 다시 돌리지 않았다 …) 모르는 것이다 — 확정이 아니다 (pending — 닫힌 쪽으로)."""
        if fid is None:
            return "value"
        f = self.field(fid)
        return "pending" if f is None else row_state(f)

    def meta_hidden(self, page_id: str | None, key: str) -> bool:
        """그 쪽의 메타 값(차량번호·작성자·장비)이 기계가 자동 적재한 것인데 사람이 그 칸을 "읽을 수 없음"으로 검수했다 — 업무 시트에도
        싣지 않는다 (양식 시트의 model.page_block 과 같은 규칙, 4.2). 쪽 메타·템플릿을 모르면 숨기지 않는다 (DB 의 값 그대로)."""
        if not page_id:
            return False
        ck = (page_id, key)
        if ck in self._extra:
            return self._extra[ck]
        src = (self.pages.meta_source.get(page_id) or {}).get(key)
        if src is None and page_id not in self.pages.meta_source:
            r = self.con.execute("SELECT source FROM doc_page_meta WHERE page_id = ? AND meta_key = ?", (page_id, key)).fetchone()
            src = r[0] if r else None
        hidden = False
        if src == "machine":
            tpl = self.site.templates.get(template_of(self, page_id) or "")
            name = next((f["name"] for f in (tpl.fields if tpl else []) if f.get("meta_key") == key), None)
            f = self.field(f"{page_id}:fields:{name}:-1") if name else None
            hidden = bool(f and f["review_status"] == "pending" and f["reviewed_by"])
        self._extra[ck] = hidden
        return hidden

    def meta(self, page_id: str | None, key: str, value):
        return None if value is None or self.meta_hidden(page_id, key) else value

    def source(self, page_id: str | None) -> str:
        if not page_id:
            return ""
        s = self.pages.source(page_id)
        if s:
            return s
        r = self.con.execute("SELECT d.source_name, p.page_no FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
                             "WHERE p.page_id = ?", (page_id,)).fetchone()
        return f"{r['source_name']}#{r['page_no']}" if r else ""

    def order(self, page_id: str) -> tuple:
        p = self.pages.index.get(page_id)
        return page_key(p["source_rel"], p["source_path"], p["document_id"], p["page_no"]) if p else ((9,), page_id)


def columns(key: str, machine_values: bool) -> list[str]:
    return [*L.COLUMNS[key], L.STATE, *([L.MACHINE] if machine_values else []), L.SOURCE, L.FIELD_ID]


def tail(ctx: Ctx, state: str, machine, source: str, fid: str | None) -> list[list]:
    out = [cell(L.ROW_STATE[state], "" if sure(state) else state)]
    if ctx.machine_values:
        out.append(cell(None if sure(state) or machine in (None, "") else machine))
    return [*out, cell(source), cell(fid)]


def value_cell(v, state: str) -> list:
    """값 열 하나: 확정이면 그 값(빈 칸이면 비운다), 아니면 비우고 표시만 (값을 적지 않는다)."""
    if state == "empty":
        return cell(None)
    return cell(v, "") if sure(state) else cell(None, state)


def machine_text(*pairs) -> str:
    """기계 값 열의 글자: (이름, 값) 들 — 값이 없으면 – ."""
    return " · ".join(f"{n} {'–' if v is None else v}" for n, v in pairs)


def in_days(con, sql: str, days: list[str], args: tuple = ()) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(days), CHUNK):
        chunk = days[i:i + CHUNK]
        out += [dict(r) for r in con.execute(sql.format(",".join("?" * len(chunk))), (*chunk, *args))]
    return out


def table(key: str, ctx: Ctx, rows: list[list]) -> dict | None:
    if not rows:
        return None
    return {"name": L.SHEETS[key], "rows": [head(*columns(key, ctx.machine_values)), *rows], "freeze": 1, "filter": True}


# ── 운반 ───────────────────────────────────────────────────────────────────
def haul_rows(ctx: Ctx, days: list[str], role: str, only_marked: bool = False) -> list[dict]:
    """prod_haul 행 (역할 하나) — 날짜, 쪽의 순서, 필드의 자리 순서. only_marked: 값이 있거나 검수 대기인 행만 (긴 표 — 4.4)."""
    rows = in_days(ctx.con, "SELECT h.*, f.row_no AS f_row, f.x0 AS f_x0, f.reviewed_by FROM prod_haul h "
                   "LEFT JOIN doc_field f ON f.field_id = h.source_field_id WHERE h.work_date IN ({}) AND h.source_role = ?",
                   days, (role,))
    rows.sort(key=lambda h: (h["work_date"], ctx.order(h["page_id"]), h["f_row"] or 0, h["f_x0"] or 0, h["haul_id"]))
    if only_marked:
        rows = [h for h in rows if h["has_value"] or h["review_status"] == "pending"]
    return rows


def haul_state(h: dict) -> str:
    return row_state({"review_status": h["review_status"], "reviewed_by": h["reviewed_by"], "has_value": h["has_value"]})


def haul_log_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    out = []
    for h in haul_rows(ctx, days, "log"):
        st = haul_state(h)
        out.append([cell(h["work_date"]), cell(h["slot"] or L.UNRESOLVED_SLOT), cell(ctx.meta(h["page_id"], "vehicle_no", h["vehicle_no"])),
                    cell(ctx.meta(h["page_id"], "operator", h["operator"])),
                    cell(h["material"]), cell(h["level"]), cell(L.SHIFT.get(h["shift"], h["shift"])),
                    value_cell(h["trips"], st), *tail(ctx, st, h["trips_raw"], ctx.source(h["page_id"]), h["source_field_id"])])
    return table("haul_log", ctx, out)


def haul_matrix_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    out = []
    for h in haul_rows(ctx, days, "matrix"):
        st = haul_state(h)
        out.append([cell(h["work_date"]), cell(h["slot"]), cell(h["vehicle_no"]), cell(h["operator"]), cell(h["material"]),
                    cell(h["level"]), value_cell(h["trips"], st),
                    *tail(ctx, st, h["trips_raw"], ctx.source(h["page_id"]), h["source_field_id"])])
    return table("haul_matrix", ctx, out)


def haul_long_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    """월별의 긴 표 — 운반(일보·행렬): 값이 있거나 검수 대기인 행만 (빈 칸 행은 싣지 않는다 — 4.4)."""
    out = []
    for role in ("log", "matrix"):
        for h in haul_rows(ctx, days, role, only_marked=True):
            st = haul_state(h)
            hide = role == "log"                                # 행렬의 차량번호·작성자는 인쇄된 머리글 — 쪽 메타가 아니다
            out.append([cell(h["work_date"]), cell(L.ROLE[role]), cell(h["slot"] or L.UNRESOLVED_SLOT),
                        cell(ctx.meta(h["page_id"], "vehicle_no", h["vehicle_no"]) if hide else h["vehicle_no"]),
                        cell(ctx.meta(h["page_id"], "operator", h["operator"]) if hide else h["operator"]),
                        cell(h["material"]), cell(h["level"]), cell(L.SHIFT.get(h["shift"], h["shift"])),
                        value_cell(h["trips"], st),
                        *tail(ctx, st, h["trips_raw"], ctx.source(h["page_id"]), h["source_field_id"])])
    if not out:
        return None
    cols = [*L.HAUL_LONG_COLUMNS, L.STATE, *([L.MACHINE] if ctx.machine_values else []), L.SOURCE, L.FIELD_ID]
    return {"name": L.LONG["haul"], "rows": [head(*cols), *out], "freeze": 1, "filter": True}


def unresolved_pages(ctx: Ctx, day: str) -> dict[str, list[str]]:
    """그날 자리 미정인 일보 쪽 → 교차검증의 키(unresolved:<차량번호 또는 쪽 ID> — validate/crosscheck.py 와 같은 규칙). 키 → 쪽들."""
    rows = ctx.con.execute("SELECT DISTINCT page_id FROM prod_haul WHERE work_date = ? AND source_role = 'log' AND slot IS NULL",
                           (day,)).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        pid = r["page_id"]
        m = ctx.con.execute("SELECT value FROM doc_page_meta WHERE page_id = ? AND meta_key = 'vehicle_no'", (pid,)).fetchone()
        if m is None:                                         # 쪽 메타가 없으면 운반 행의 값 (crosscheck 와 같다)
            m = ctx.con.execute("SELECT vehicle_no AS value FROM prod_haul WHERE page_id = ? LIMIT 1", (pid,)).fetchone()
        key = f"unresolved:{(m['value'] if m else None) or pid}"
        out.setdefault(key, []).append(pid)
    for k in out:
        out[k].sort(key=ctx.order)
    return out


def _xcheck_rows_of(x: dict, unres: dict[str, list[str]], by_key: dict, ctx: Ctx) -> tuple[list[dict], list[str]]:
    """교차검증 행 하나가 견준 운반 행과 일보 쪽: 자리 미정이면 그 쪽(들)의 일보 행, 아니면 그 자리의 일보·행렬 행."""
    pages = unres.get(x["slot"])
    if pages is not None:
        return [h for pid in pages for h in by_key.get(("log", None, pid, x["material"], x["level"]), [])], pages
    hs = [h for (_role, slot, _pid, m, lv), v in by_key.items() if slot == x["slot"] and m == x["material"]
          and lv == x["level"] for h in v]
    return hs, sorted({h["page_id"] for h in hs if h["source_role"] == "log"}, key=ctx.order)


def xcheck_haul_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    out = []
    for day in days:
        rows = [dict(r) for r in ctx.con.execute("SELECT * FROM xcheck_haul WHERE work_date = ?", (day,))]
        if not rows:
            continue
        unres = unresolved_pages(ctx, day)
        haul = in_days(ctx.con, "SELECT h.page_id, h.slot, h.source_role, h.material, h.level, h.review_status, h.has_value, "
                       "f.reviewed_by FROM prod_haul h LEFT JOIN doc_field f ON f.field_id = h.source_field_id "
                       "WHERE h.work_date IN ({})", [day])
        by_key: dict[tuple, list[dict]] = {}
        for h in haul:
            by_key.setdefault((h["source_role"], h["slot"], h["page_id"], h["material"], h["level"]), []).append(h)
        # 자리가 정해진 행이 먼저(자리 순), 자리 미정은 그 쪽의 순서대로
        rows.sort(key=lambda x: (x["slot"] in unres, ctx.order(unres[x["slot"]][0]) if x["slot"] in unres else (),
                                 "" if x["slot"] in unres else x["slot"], x["material"], x["level"]))
        for x in rows:
            hs, pids = _xcheck_rows_of(x, unres, by_key, ctx)
            st = worst(haul_state(h) for h in hs)
            if not hs and (x["log_has"] or x["matrix_has"] or str(x["slot"]).startswith("unresolved:")):
                st = "pending"                              # 견준 운반 행을 찾지 못했다 — 확정이라고 말하지 않는다 (닫힌 쪽으로)
            ok = sure(st)
            verdict = L.XCHECK_STATUS.get(x["status"], x["status"]) + ("" if ok else L.PROVISIONAL)
            unresolved = x["slot"] in unres
            machine = None if ok else machine_text(("일보", x["log_trips_raw"]), ("행렬", x["matrix_trips_raw"]))
            hid = {k: any(ctx.meta_hidden(p, k) for p in pids) for k in ("vehicle_no", "operator")}
            out.append([cell(day), cell(L.UNRESOLVED_SLOT if unresolved else x["slot"]),
                        cell(None if unresolved or hid["vehicle_no"] else x["vehicle_no"]),
                        cell(None if unresolved or hid["operator"] else x["operator"]),
                        cell(x["material"]), cell(x["level"]),
                        value_cell(x["log_trips"], st), value_cell(x["matrix_trips"], st),
                        cell(verdict, "mismatch" if x["status"] == "mismatch" else ""),
                        *tail(ctx, "value" if ok else st, machine, ", ".join(ctx.source(p) for p in pids), None)])
    return table("xcheck_haul", ctx, out)


def assignment_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    out = []
    for x in sorted(in_days(ctx.con, "SELECT * FROM eq_assignment_obs WHERE work_date IN ({})", days),
                    key=lambda x: (x["work_date"], x["slot"])):
        pids = [r["page_id"] for r in ctx.con.execute(
            "SELECT DISTINCT page_id FROM prod_haul WHERE work_date = ? AND source_role = 'log' AND slot = ?", (x["work_date"], x["slot"]))]
        hid = {k: any(ctx.meta_hidden(p, k) for p in pids) for k in ("vehicle_no", "operator")}
        out.append([cell(x["work_date"]), cell(x["slot"]), cell(None if hid["vehicle_no"] else x["vehicle_no"]),
                    cell(None if hid["operator"] else x["operator"]), cell(x["header_vehicle_no"]),
                    cell(x["header_operator"]), cell(L.MATCHED_BY.get(x["matched_by"], x["matched_by"])),
                    cell(L.YES if x["header_mismatch"] else None, "mismatch" if x["header_mismatch"] else ""),
                    *tail(ctx, "value", None, ", ".join(ctx.source(p) for p in sorted(pids, key=ctx.order)), None)])
    return table("assignment", ctx, out)


# ── 가동 일보 ───────────────────────────────────────────────────────────────
def shift_fields(ctx: Ctx, page_id: str, template_name: str) -> list[dict]:
    """그 쪽의 근무 시각 칸 (handlers.usage.is_shift_cell — 핸들러와 같은 규칙)."""
    tpl = ctx.site.templates.get(template_name)
    if tpl is None:
        return []
    roles = {reg["name"]: reg.get("role") for reg in tpl.regions}
    fields = ctx.pages.by_page.get(page_id) or {
        r["field_id"]: dict(r) for r in ctx.con.execute("SELECT * FROM doc_field WHERE page_id = ?", (page_id,))}
    return [f for f in fields.values() if is_shift_cell(roles.get(f["region"]), f)]


def usage_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    out = []
    for u in sorted(in_days(ctx.con, "SELECT * FROM eq_usage_daily WHERE work_date IN ({})", days),
                    key=lambda u: (u["work_date"], ctx.order(u["page_id"]))):
        sf = {k: ctx.state(u[f"{k}_field_id"]) for k in ("start", "end", "total")}
        shifts = shift_fields(ctx, u["page_id"], u["source_form"])
        sh = worst(row_state(f) for f in shifts) if shifts else "value"
        basis = u["hours_basis"]
        if basis in ("meter", "clock"):
            hs = worst([sf["start"], sf["end"]])
        elif basis == "total":
            hs = worst([sf["total"], sf["start"], sf["end"]])
        elif basis == "shifts":
            hs = sh
        else:
            hs = "value"
        shown = [sf[k] for k in ("start", "end", "total") if u[f"{k}_field_id"]] + ([sh] if shifts else [])
        st = worst(shown) if shown else "value"
        try:
            shift_text = "; ".join(f"{k}: {v}" for k, v in json.loads(u["shifts"]).items()) if u["shifts"] else None
        except ValueError:
            shift_text = u["shifts"]
        raw = " · ".join(f"{n} {u[f'meter_{k}_raw']}" for k, n in (("start", "시작"), ("end", "종료"), ("total", "총"))
                         if u[f"meter_{k}_raw"] not in (None, "") and not sure(sf[k]))
        fids = " ".join(x for x in (u["start_field_id"], u["end_field_id"], u["total_field_id"]) if x)
        out.append([cell(u["work_date"]), cell(u["source_form"]), cell(ctx.meta(u["page_id"], "equipment", u["equipment"])),
                    cell(ctx.meta(u["page_id"], "operator", u["operator"])),
                    cell(L.READING_KIND.get(u["reading_kind"], u["reading_kind"])),
                    value_cell(u["meter_start"], sf["start"]), value_cell(u["meter_end"], sf["end"]),
                    value_cell(u["meter_total"], sf["total"]),
                    value_cell(u["clock_start"], sf["start"]), value_cell(u["clock_end"], sf["end"]),
                    value_cell(shift_text, sh), value_cell(u["shift_minutes"], sh),
                    cell(u["activity_rows"]), cell(None if u["signed"] is None else (L.YES if u["signed"] else "")),
                    value_cell(u["hours"], hs), cell(L.HOURS_BASIS.get(basis, basis) if sure(hs) else None),
                    *tail(ctx, st, raw or None, ctx.source(u["page_id"]), fids or None)])
    return table("usage", ctx, out)


def tally_sheet(ctx: Ctx, days: list[str], only_marked: bool = False) -> dict | None:
    rows = in_days(ctx.con, "SELECT t.*, f.row_no AS f_row, f.x0 AS f_x0, f.reviewed_by FROM prod_tally t "
                   "LEFT JOIN doc_field f ON f.field_id = t.source_field_id WHERE t.work_date IN ({})", days)
    rows.sort(key=lambda t: (t["work_date"], ctx.order(t["page_id"]), t["f_row"] or 0, t["f_x0"] or 0, t["tally_id"]))
    if only_marked:
        rows = [t for t in rows if t["has_value"] or t["review_status"] == "pending"]
    out = []
    for t in rows:
        st = row_state({"review_status": t["review_status"], "reviewed_by": t["reviewed_by"], "has_value": t["has_value"]})
        out.append([cell(t["work_date"]), cell(t["source_form"]), cell(ctx.meta(t["page_id"], "equipment", t["equipment"])),
                    cell(t["item"]), cell(t["place"]),
                    cell(t["column_name"]), cell(L.SHIFT.get(t["shift"], t["shift"])),
                    cell(L.SUBTOTAL_MARK if t["is_subtotal"] else None),
                    value_cell(t["count"], st), *tail(ctx, st, t["count_raw"], ctx.source(t["page_id"]), t["source_field_id"])])
    return table("tally", ctx, out)


def xcheck_usage_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    out = []
    kinds = {k: i for i, k in enumerate(L.CHECK_KIND)}
    rows = in_days(ctx.con, "SELECT x.*, u.equipment FROM xcheck_usage x LEFT JOIN eq_usage_daily u ON u.page_id = x.page_id "
                   "WHERE x.work_date IN ({})", days)
    for x in sorted(rows, key=lambda x: (x["work_date"], ctx.order(x["page_id"]), kinds.get(x["check_kind"], 9), x["item"])):
        try:
            cells = check_cells(ctx.con, ctx.site, x)
        except KeyError:                                    # 템플릿에서 그 표가 없어졌다 (고친 뒤 다시 돌리지 않았다)
            cells = None
        st = worst(ctx.state(fid) for fid in cells) if cells else "pending"
        if x["check_kind"] == "subtotal" and (ctx.site.templates.get(template_of(ctx, x["page_id"])) is None or len(cells or ()) < 2):
            st = "pending"                                  # 더한 칸을 모른다 (템플릿이 없거나 소계의 모양이 바뀌었다) — 닫힌 쪽으로
        ok = sure(st)
        # 견준 값에 기대는 결과에만 "(잠정)" — 첫 기록·모름(장비를 모른다)은 값과 상관없다
        provisional = not ok and x["result"] not in ("first", "unknown")
        result = L.CHECK_RESULT.get(x["result"], x["result"]) + (L.PROVISIONAL if provisional else "")
        out.append([cell(x["work_date"]), cell(L.CHECK_KIND.get(x["check_kind"], x["check_kind"])),
                    cell(ctx.meta(x["page_id"], "equipment", x["equipment"])),
                    value_cell(x["value_a"], st), value_cell(x["value_b"], st), value_cell(x["diff"], st),
                    cell(x["days_between"]),
                    cell(result, "mismatch" if x["result"] in ("mismatch", "gap", "overlap") and ok else ""),
                    cell(ctx.source(x["other_page_id"]) or None),
                    *tail(ctx, "value" if ok else st, None, ctx.source(x["page_id"]), x["field_a"])])
    return table("xcheck_usage", ctx, out)


# ── 점검 ───────────────────────────────────────────────────────────────────
def template_of(ctx: Ctx, page_id: str | None) -> str | None:
    p = ctx.pages.index.get(page_id) if page_id else None
    if p is not None:
        return p["template_name"]
    r = ctx.con.execute("SELECT template_name FROM doc_page WHERE page_id = ?", (page_id,)).fetchone() if page_id else None
    return r[0] if r else None


def page_overrides(ctx: Ctx, page_id: str, tpl) -> dict[str, str]:
    """그 쪽에서 핸들러가 고쳐 말한 칸 상태 (FormHandler.export_cells — 양식 시트와 같은 것을 업무 시트도 쓴다)."""
    key = ("overrides", page_id)
    if key not in ctx._extra:
        from ..handlers import get_handler

        fields = ctx.pages.by_page.get(page_id) or {
            r["field_id"]: dict(r) for r in ctx.con.execute("SELECT * FROM doc_field WHERE page_id = ?", (page_id,))}
        try:
            handler = get_handler(tpl.handler)
        except KeyError:                                    # 등록되지 않은 핸들러 — 고쳐 말하지 않는다 (model.page_block 과 같다)
            handler = None
        ctx._extra[key] = handler.export_cells(tpl, fields)[0] if handler is not None else {}
    return ctx._extra[key]


def inspection_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    """점검: 이상 유·무는 그 행의 두 ✓ 칸이 확정일 때만 — 점검하지 않은 쪽(핸들러가 ✓ 칸을 비운 쪽)은 확정된 빈 칸이라 판정 불가."""
    out = []
    rows = in_days(ctx.con, "SELECT i.*, e.hid, p.template_name FROM insp_daily i LEFT JOIN eq_equipment e "
                   "ON e.equipment_id = i.equipment_id LEFT JOIN doc_page p ON p.page_id = i.page_id WHERE i.inspection_date IN ({})",
                   days)
    rows_of: dict[str, dict[str, int]] = {}                 # 템플릿 → 장비 ID → 행 번호
    for x in rows:
        tn = x["template_name"]
        if tn not in rows_of:
            tpl = ctx.site.templates.get(tn)
            rows_of[tn] = {} if tpl is None or tpl.handler != "inspection" else {
                equipment_id(str(r.get("key", ""))): r["row"] for r in tpl.region(layout(tpl)[0])["rows"] if is_equipment_row(r)}
    for x in sorted(rows, key=lambda x: (x["inspection_date"], ctx.order(x["page_id"] or ""),
                                         rows_of.get(x["template_name"], {}).get(x["equipment_id"], 0), x["equipment_id"])):
        tpl = ctx.site.templates.get(x["template_name"])
        row = rows_of.get(x["template_name"], {}).get(x["equipment_id"])
        if tpl is not None and row is not None and x["page_id"]:
            region, yes_col, no_col, _ = layout(tpl)
            over = page_overrides(ctx, x["page_id"], tpl)
            ids = [f"{x['page_id']}:{region}:{c}:{row}" for c in (yes_col, no_col)]
            mark_state = worst("empty" if over.get(i) == "empty" else ctx.state(i) for i in ids)
        else:                                               # 템플릿·행을 모른다 — 그 행의 상태로 (닫힌 쪽으로)
            mark_state = "pending" if x["review_status"] == "pending" else "value"
        rem_state = ctx.state(x["source_field_id"])
        st = worst(["value" if mark_state == "empty" else mark_state, "value" if rem_state == "empty" else rem_state])
        abnormal = L.ABNORMAL.get(x["abnormal"], x["abnormal"]) if sure(mark_state) else None
        out.append([cell(x["inspection_date"]), cell(x["hid"] or x["equipment_id"]),
                    cell(abnormal, "" if sure(mark_state) else mark_state),
                    value_cell(x["remark"] if x["remark"] not in (None, "") else None, rem_state),
                    *tail(ctx, st, None, ctx.source(x["page_id"]), x["source_field_id"])])
    return table("inspection", ctx, out)


def daily_business(ctx: Ctx, day: str) -> list[dict]:
    """그 날짜에 행이 있는 업무 시트만 (4.3 의 순서)."""
    days = [day]
    sheets = [haul_log_sheet(ctx, days), haul_matrix_sheet(ctx, days), xcheck_haul_sheet(ctx, days), assignment_sheet(ctx, days),
              usage_sheet(ctx, days), tally_sheet(ctx, days), xcheck_usage_sheet(ctx, days), inspection_sheet(ctx, days)]
    return [s for s in sheets if s is not None]
