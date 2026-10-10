"""월별 파일의 모델 (tasks/0008 4.4): 날짜별 요약, 운반 표, 배차, 긴 표.

`OUT/monthly/<YYYY-MM>.xlsx`. 그 달에 쪽이 있는 날짜를 모은다.

**운반 표**: 행 = 날짜 × 주·야(주간 → 야간, shift 가 없는 양식은 한 줄), 열 = 자리(slot)마다 한 블록 × 광종·편, 블록 끝에 합계.
값은 일보(prod_haul.source_role = 'log')의 횟수다. 칸의 상태는 4.2 의 것(값·빈 칸·검수 대기·판독 불가)에 둘을 더한다:
  문서 없음 –   그 날짜·자리의 일보 쪽이 없고, 그 날짜에 자리 미정인 일보 쪽도 없다
  모름 –?      그 날짜·자리의 일보 쪽이 없는데 자리 미정인 일보 쪽이 있다 (그 쪽이 이 자리의 것일 수 있다)
- 자리 미정인 일보 쪽(운반 행의 slot 이 NULL)은 오른쪽의 "자리 미정 k" 블록 — 그 날짜의 자리 미정 쪽 가운데 쪽의 순서로 k 번째.
  블록의 수 = 그 달에서 하루에 가장 많았던 수. 블록마다 첫 열이 출처(파일명#쪽). 잃지 않는다.
- 교차검증이 불일치인 날짜·자리·광종·편의 칸은 표시한다 (주·야 두 칸 다 — 그 판정이 확정일 때만, 잠정인 불일치는 교차검증 시트에서
  "(잠정)"으로 본다). 고치지 않는다 — 일보의 값 그대로 (ADR 0006).
- 블록의 합계는 그 줄의 칸이 전부 확정(값·빈 칸)일 때만 (값으로 — 수식이 아니다). 줄 끝의 합계(차량을 가로지른 합)는 두지 않는다.
- 광종·편의 순서와 표시 이름: 일보 템플릿의 행 순서와 행의 display. 자리의 순서: 자리 이름. 사이트 팩 site.toml 의
  [haul_table] columns = ["ORE|L0", …] · slots = ["T01", …] 가 바꾼다 — 거기에 없는 것은 뒤에 붙인다 (빠뜨리지 않는다).
"""
from __future__ import annotations

import sqlite3
from collections import Counter

from ..forms.template import display_of
from . import labels as L
from .business import (
    Ctx,
    assignment_sheet,
    haul_long_sheet,
    haul_rows,
    haul_state,
    inspection_sheet,
    tally_sheet,
    usage_sheet,
    xcheck_haul_sheet,
    xcheck_usage_sheet,
)
from .daily import summary_rows
from .model import Pages, cell, finish, form_sheets, head, load_pages, sure

SHIFT_ORDER = {"day": 0, "night": 1, None: 2}


def haul_columns(site, data_keys: set[str]) -> list[tuple[str, str]]:
    """운반 표의 광종·편 열: (키 "광종|편", 표시 이름). 일보 템플릿의 행 순서(이름이 앞인 템플릿부터) → [haul_table] columns 가
    앞에 오게 → 데이터에만 있는 것은 뒤에 (정렬)."""
    cols: dict[str, str] = {}
    for tpl in sorted(site.templates.values(), key=lambda t: t.name):
        if tpl.handler != "haul" or (tpl.handler_options or {}).get("role", "log") != "log":
            continue
        name = (tpl.handler_options or {}).get("region") or (tpl.regions[0]["name"] if tpl.regions else None)
        try:
            reg = tpl.region(name)
        except KeyError:
            continue
        for r in sorted(reg["rows"], key=lambda r: r["row"]):
            if "material" in r and "level" in r:
                key = f"{r['material']}|{r['level']}"
                cols.setdefault(key, display_of(r, key))
    for key in sorted(data_keys - set(cols)):
        cols[key] = key
    want = [k for k in site_order(site, "columns") if k in cols]
    return [(k, cols[k]) for k in [*want, *(k for k in cols if k not in want)]]


def site_order(site, key: str) -> list[str]:
    """site.toml [haul_table] columns·slots (검사는 SitePack 이 읽을 때 — forms/sitepack._haul_table)."""
    return list((getattr(site, "haul_table", None) or {}).get(key) or [])


def haul_table_sheet(ctx: Ctx, days: list[str]) -> dict | None:
    logs = haul_rows(ctx, days, "log")
    if not logs:
        return None
    # 칸 하나에 행의 목록 — 한 쪽에 같은 (광종|편, 주야)의 행이 둘 이상이면 겹침이다 (template check 가 오류로 내는 템플릿 — 검사 전에 만든
    # 것). 덮어쓰면 뒤의 행이 앞의 행을 지운다 (합성: 값 칸 71 → 70, 합 309 → 305, 그 줄의 합계는 확정으로 나왔다 — tasks/0009 1절 다)
    by_slot: dict[tuple, dict] = {}                         # (날짜, 자리) → {(광종|편, 주야): [행]}
    unresolved: dict[str, dict[str, dict]] = {}             # 날짜 → 쪽 → {(광종|편, 주야): [행]}
    for h in logs:
        k = (f"{h['material']}|{h['level']}", h["shift"])
        if h["slot"] is None:
            unresolved.setdefault(h["work_date"], {}).setdefault(h["page_id"], {}).setdefault(k, []).append(h)
        else:
            by_slot.setdefault((h["work_date"], h["slot"]), {}).setdefault(k, []).append(h)
    matrix_slots = {r[0] for r in ctx.con.execute(
        f"SELECT DISTINCT slot FROM prod_haul WHERE source_role = 'matrix' AND work_date IN ({','.join('?' * len(days))})", days)}
    slots_seen = {s for _d, s in by_slot} | {s for s in matrix_slots if s}
    want = [s for s in site_order(ctx.site, "slots") if s in slots_seen]
    slots = [*want, *sorted(s for s in slots_seen if s not in want)]
    cols = haul_columns(ctx.site, {f"{h['material']}|{h['level']}" for h in logs})
    # 주야: 주간 → 야간 → 없음, 그 밖의 이름은 이름 순 (집합을 도는 순서는 프로세스마다 다르다 — 파일을 다시 쓰게 된다)
    shifts = sorted({h["shift"] for h in logs}, key=lambda s: (SHIFT_ORDER.get(s, 3), "" if s is None else str(s)))
    k_max = max((len(p) for p in unresolved.values()), default=0)
    # 교차검증이 불일치인 칸 — 그 판정이 확정일 때만 (견준 운반 행이 전부 확정 — 교차검증 시트의 "(잠정)" 과 같은 규칙).
    # 잠정인 불일치는 표시하지 않는다 (확정되지 않은 판정을 확정처럼 보이지 않게)
    unsure = {(h["work_date"], h["slot"], f"{h['material']}|{h['level']}") for role in ("log", "matrix")
              for h in haul_rows(ctx, days, role) if not sure(haul_state(h))}
    mismatch = {k for k in ((r["work_date"], r["slot"], f"{r['material']}|{r['level']}") for r in ctx.con.execute(
        f"SELECT work_date, slot, material, level FROM xcheck_haul WHERE status = 'mismatch' AND work_date IN "
        f"({','.join('?' * len(days))})", days)) if k not in unsure}

    top = [cell(), cell()]
    names = head(*L.HAUL_TABLE_HEAD)
    for s in slots:
        top += [cell(s, "title"), *(cell() for _ in cols)]
        names += head(*(n for _k, n in cols), L.HAUL_TABLE_SUM)
    for k in range(1, k_max + 1):
        top += [cell(f"{L.UNRESOLVED_SLOT} {k}", "title"), *(cell() for _ in range(len(cols) + 1))]
        names += head(L.SOURCE, *(n for _k, n in cols), L.HAUL_TABLE_SUM)
    rows = [top, names]
    seen_days = sorted({h["work_date"] for h in logs} | set(days))
    for day in seen_days:
        pages = sorted(unresolved.get(day, {}), key=ctx.order)
        for shift in shifts:
            row = [cell(day), cell(L.SHIFT.get(shift, shift))]
            for s in slots:
                got = by_slot.get((day, s))
                if got is None:
                    mark = (L.UNKNOWN_MARK, "unknown") if pages else (L.NO_DOC_MARK, "no_doc")
                    row += [cell(*mark) for _ in cols] + [cell()]
                    continue
                row += line(got, cols, shift, lambda key, s=s, day=day: (day, s, key) in mismatch)
            for k in range(k_max):
                if k < len(pages):
                    got = unresolved[day][pages[k]]
                    row += [cell(ctx.source(pages[k])), *line(got, cols, shift, lambda key: False)]
                else:
                    row += [cell() for _ in range(len(cols) + 2)]
            rows.append(row)
    return {"name": L.SHEETS["haul_table"], "rows": rows, "freeze": 2, "filter": False}


def has_overlap(sheet: dict | None) -> bool:
    """운반 표에 겹침 칸이 있나 (범례에 그 줄을 더할 때만 — 겹침이 없는 파일의 모델은 그대로)."""
    return bool(sheet) and any(len(c) > 1 and c[1] and c[1].split()[0] == "overlap" for r in sheet["rows"] for c in r)


def line(got: dict, cols: list[tuple[str, str]], shift, is_mismatch) -> list[list]:
    """한 블록의 한 줄: 광종·편마다 칸 + 합계 (그 줄이 전부 확정이고 겹침이 없을 때만)."""
    out, total, sure_all = [], 0, True
    for key, _name in cols:
        hs = got.get((key, shift))
        flag = " mismatch" if is_mismatch(key) else ""
        if hs is None:                                       # 그 일보에 이 광종·편의 칸이 없다
            out.append(cell(None, flag.strip()))
            continue
        if len(hs) > 1:                                      # 겹침: 더하지 않는다 — 그 줄의 합계도 비운다
            sure_all = False
            out.append(cell(L.OVERLAP_MARK, "overlap" + flag))
            continue
        h = hs[0]
        st = haul_state(h)
        if not sure(st):
            sure_all = False
            out.append(cell(L.ILLEGIBLE_MARK if st == "illegible" else L.PENDING_MARK, st + flag))
        elif st == "empty":
            out.append(cell(None, ("empty" + flag)))
        elif h["trips"] is None:                             # 글씨는 있는데 수가 없다 (확정) — 유무만
            sure_all = False
            out.append(cell(L.PRESENT_MARK, "present" + flag))
        else:
            total += h["trips"]
            out.append(cell(h["trips"], "value" + flag))
    out.append(cell(total, "value") if sure_all else cell(None, "pending"))
    return out


def day_summary_sheet(ctx: Ctx, month: str, days: list[str], pending: Counter, by_family: Counter, waiting: int) -> dict:
    """날짜별 요약 (4.4) + 요약 시트의 머리(달, 사본이라는 한 줄, 만든 시각·판)와 범례. pending: 날짜 → 검수 대기 칸,
    by_family: 계열 → 그 달의 검수 대기 칸."""
    con = ctx.con
    rows = summary_rows(con, ctx.site, L.SEC_MONTH, month, ctx.pages, by_family, days, waiting)
    legend_at = next(i for i, r in enumerate(rows) if r and r[0] == [L.NO_DATE_NOTE, "note"])
    table = [[], head(*L.MONTH_DAY_COLUMNS)]
    for day in days:
        pages = [p for p in ctx.pages.all if p["work_date"] == day]
        x = Counter(r[0] for r in con.execute("SELECT status FROM xcheck_haul WHERE work_date = ?", (day,)))
        u = con.execute("SELECT COUNT(*) FROM xcheck_usage WHERE work_date = ? AND result IN ('mismatch', 'gap', 'overlap')",
                        (day,)).fetchone()[0]
        unres = con.execute("SELECT COUNT(DISTINCT page_id) FROM prod_haul WHERE work_date = ? AND source_role = 'log' "
                            "AND slot IS NULL", (day,)).fetchone()[0]
        table.append([cell(day), cell(len(pages)), cell(sum(p["status"] != "loaded" for p in pages)), cell(pending.get(day, 0)),
                      cell(x["match"]), cell(x["mismatch"]), cell(x["missing_log"] + x["missing_matrix"]), cell(u), cell(unres)])
    return {"name": L.SUMMARY, "rows": rows[:legend_at] + table + [[]] + rows[legend_at:]}


def monthly_book(con: sqlite3.Connection, site, month: str, days: list[str], machine_values: bool = False,
                 pages: Pages | None = None) -> dict:
    """그 달의 월별 파일 모델. days: 그 달에 쪽이 있는 ISO 날짜들. 한 읽기 트랜잭션 안에서 부른다. pages: 이미 읽은 그 날짜들의 쪽
    (load_pages(con, days) — 일별 파일과 같이 쓴다). 없으면 읽는다."""
    if pages is None:
        pages = load_pages(con, days)
    ctx = Ctx(con, site, pages, machine_values)
    pending, by_family = Counter(), Counter()
    for day in days:                                          # 검수 대기 칸의 수 — 일별 파일의 양식 시트와 같은 수
        fams = form_sheets(site, pages.day(day))[1]          # 그달에 읽은 행에서 (다시 읽지 않는다)
        pending[day] = sum(fams.values())
        by_family += fams
    waiting = con.execute(
        f"SELECT COUNT(*) FROM doc_document d WHERE (d.status = 'received' OR d.work_requested > d.work_done) AND "
        f"(substr(d.work_date, 1, 7) = ? OR EXISTS (SELECT 1 FROM doc_page p WHERE p.document_id = d.document_id AND "
        f"p.work_date IN ({','.join('?' * len(days))})))", (month, *days)).fetchone()[0]
    summary = day_summary_sheet(ctx, month, days, pending, by_family, waiting)
    longs = []
    for key, s in (("haul", haul_long_sheet(ctx, days)), ("xcheck_haul", xcheck_haul_sheet(ctx, days)),
                   ("usage", usage_sheet(ctx, days)), ("tally", tally_sheet(ctx, days, only_marked=True)),
                   ("xcheck_usage", xcheck_usage_sheet(ctx, days)), ("inspection", inspection_sheet(ctx, days))):
        if s is not None:
            s["name"] = L.LONG[key]
            longs.append(s)
    assign = assignment_sheet(ctx, days)
    if assign is not None:
        assign["name"] = L.LONG["assignment"]
    table = haul_table_sheet(ctx, days)
    if has_overlap(table):                                    # 범례의 겹침 줄은 겹침이 있는 파일에만
        summary["rows"].append([cell(), cell(L.LEGEND_OVERLAP[1], L.LEGEND_OVERLAP[0]), cell(L.LEGEND_OVERLAP[2])])
    sheets = [summary, table, assign, *longs]
    return finish("monthly", month, [s for s in sheets if s is not None])
