"""일별 파일의 모델 (tasks/0008 4.3): 요약, 양식마다 한 장(종이와 같은 행·열), 업무 표.

`OUT/daily/<YYYY-MM>/<YYYY-MM-DD>.xlsx`. 날짜는 쪽의 날짜(doc_page.work_date)다 — 그 날짜의 쪽이 하나라도 있으면 파일이 있다.
"""
from __future__ import annotations

import sqlite3
from collections import Counter

from . import labels as L
from .business import Ctx, daily_business
from .model import (
    Pages,
    cell,
    family_of,
    family_title,
    finish,
    form_sheets,
    head,
    load_pages,
    waiting_documents,
)

PAGE_ORDER = tuple(L.PAGE_STATUS)


def summary_rows(con: sqlite3.Connection, site, key_label: str, key: str, pages, pending: Counter, days: list[str],
                 waiting: int) -> list[list]:
    """요약 시트 (일별·월별이 같은 모양): 구분 | 항목 | 수."""
    rows: list[list] = [[cell(key_label, "h"), cell(key)], [cell(L.COPY_NOTE, "note")],
                        [cell(L.SEC_STAMP_TIME, "h"), cell(None, "stamp_time")],
                        [cell(L.SEC_STAMP_VERSION, "h"), cell(None, "stamp_version")], [], head(*L.SUMMARY_HEAD)]
    status = Counter(p["status"] for p in pages.all)
    for s in sorted(status, key=lambda s: (PAGE_ORDER.index(s) if s in PAGE_ORDER else 99, s)):
        rows.append([cell(L.SEC_PAGES), cell(L.PAGE_STATUS.get(s, s)), cell(status[s])])
    rows.append([cell(L.SEC_PAGES), cell(L.TOTAL), cell(len(pages.all))])
    forms = Counter(family_of(site, p["template_name"]) for p in pages.loaded)
    for fam in forms:
        rows.append([cell(L.SEC_FORMS), cell(family_title(site, fam)), cell(forms[fam])])
    for fam in forms:
        rows.append([cell(L.SEC_PENDING), cell(family_title(site, fam)), cell(pending.get(fam, 0))])
    rows.append([cell(L.SEC_PENDING), cell(L.TOTAL), cell(sum(pending.values()))])
    marks = ",".join("?" * len(days))
    for r in con.execute(f"SELECT status, COUNT(*) AS n FROM xcheck_haul WHERE work_date IN ({marks}) GROUP BY status "
                         "ORDER BY status", days):
        rows.append([cell(L.SEC_XCHECK), cell(L.XCHECK_STATUS.get(r["status"], r["status"])), cell(r["n"])])
    for r in con.execute(f"SELECT check_kind, result, COUNT(*) AS n FROM xcheck_usage WHERE work_date IN ({marks}) "
                         "GROUP BY check_kind, result ORDER BY check_kind, result", days):
        rows.append([cell(L.SEC_XUSAGE), cell(f"{L.CHECK_KIND.get(r['check_kind'], r['check_kind'])} — "
                                              f"{L.CHECK_RESULT.get(r['result'], r['result'])}"), cell(r["n"])])
    rows.append([cell(L.SEC_WAITING), cell(L.WAITING_NOTE if waiting else None, "note" if waiting else ""), cell(waiting)])
    rows += [[cell(L.NO_DATE_NOTE, "note")], [], head("표시", "보기", "뜻")]
    rows += [[cell(), cell(sample, style), cell(meaning)] for style, sample, meaning in L.LEGEND]
    return rows


def daily_book(con: sqlite3.Connection, site, day: str, machine_values: bool = False, pages: Pages | None = None) -> dict:
    """그 날짜의 일별 파일 모델. 한 읽기 트랜잭션 안에서 부른다 (store.db.read_txn — 표마다 다른 시점을 보지 않게).
    pages: 이미 읽은 그 날짜의 쪽 (그 달을 읽은 것의 Pages.day — 같은 모델이다). 없으면 읽는다."""
    if pages is None:
        pages = load_pages(con, [day])
    forms, pending = form_sheets(site, pages)
    biz = daily_business(Ctx(con, site, pages, machine_values), day)
    summary = {"name": L.SUMMARY, "rows": summary_rows(con, site, L.SEC_DATE, day, pages, pending, [day],
                                                       waiting_documents(con, day))}
    return finish("daily", day, [summary, *forms, *biz])
