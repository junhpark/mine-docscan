"""✓ 판정의 평가 (eval --checks, tasks/0004 단계 6): 기계의 답 대 검수로 정한 행의 답.

기계의 답(유 / 무 / 표시 없음 / 판정 불가)과 정답(유 / 무 / 표시 없음 / 모름)은 review/checks.py 의 규칙.
  · 표: 기계의 답 × 정답, 정답이 있는 행 전부 (모름 포함)
  · 기계가 판정한 행(유·무·표시 없음) 중 맞은 비율과 윌슨 구간 — 정답이 모름인 행은 뺀다
  · 판정 불가의 비율, 판정 불가였던 행의 정답 분포
  · 쪽(날짜) 단위로 column_unused 가 맞았는지: 기계 = 쪽 전체가 표시 없음, 정답 = 검수한 행(모름 제외)이 전부 표시 없음
값(등록번호·이름)은 내지 않는다 — 날짜와 수만.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from collections import Counter

from ..review.checks import NO, NONE, UNDECIDED, UNKNOWN, YES, answers, check_rows, machine_answers
from .stats import rate_with_ci

MACHINE = (YES, NO, NONE, UNDECIDED)
TRUTH = (YES, NO, NONE, UNKNOWN)


def evaluate_checks(con: sqlite3.Connection, site, split: str = "all") -> dict:
    rows = check_rows(con, site)
    if split != "all":
        rows = [c for c in rows if site.split_of(c.work_date) == split]
    truth = answers(con, rows)
    machine = machine_answers(rows)
    judged = [c for c in rows if c.item_id in truth]
    table = {m: {t: 0 for t in TRUTH} for m in MACHINE}
    for c in judged:
        table[machine[c.item_id]][truth[c.item_id]] += 1
    decided = [c for c in judged if machine[c.item_id] != UNDECIDED and truth[c.item_id] != UNKNOWN]
    right = sum(machine[c.item_id] == truth[c.item_id] for c in decided)
    undecided = [c for c in judged if machine[c.item_id] == UNDECIDED]
    pages: dict[str, dict] = {}
    for c in judged:
        if truth[c.item_id] == UNKNOWN:
            continue
        p = pages.setdefault(c.page_id, {"date": c.work_date, "rows": 0, "machine_unused": c.page_unused, "truth_unused": True})
        p["rows"] += 1
        p["truth_unused"] = p["truth_unused"] and truth[c.item_id] == NONE
    for p in pages.values():
        p["agree"] = p["machine_unused"] == p["truth_unused"]
    days = sorted(pages.values(), key=lambda p: (p["date"] or ""))
    return {"split": split, "rows": len(rows), "reviewed_rows": len(judged),
            "machine": dict(Counter(machine[c.item_id] for c in rows)),
            "table": table,
            "decided_accuracy": rate_with_ci(right, len(decided)),
            "undecided": rate_with_ci(len(undecided), len(judged)),
            "undecided_truth": {t: sum(truth[c.item_id] == t for c in undecided) for t in TRUTH},
            "column_unused": {"pages": len(days), "agree": sum(p["agree"] for p in days), "by_page": days}}


def _width(text: str) -> int:
    """터미널에서 차지하는 칸 수 (한글은 두 칸)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _ljust(text: str, w: int) -> str:
    return text + " " * max(0, w - _width(text))


def _rjust(text: str, w: int) -> str:
    return " " * max(0, w - _width(text)) + text


def format_checks(r: dict) -> str:
    w = max(_width(x) for x in MACHINE + TRUTH) + 2
    lines = [f"✓ 판정 (분할 {r['split']}): 점검표 장비 행 {r['rows']}개 중 정답(검수)이 있는 행 {r['reviewed_rows']}개",
             "  " + _ljust("기계 \\ 정답", w + 2) + "".join(_rjust(t, w) for t in TRUTH)]
    for m in MACHINE:
        lines.append("  " + _ljust(m, w + 2) + "".join(_rjust(str(r["table"][m][t]), w) for t in TRUTH))
    a, u = r["decided_accuracy"], r["undecided"]
    lines.append(f"  기계가 판정한 행(유·무·표시 없음, 정답 모름 제외) 중 맞은 것 {a['k']}/{a['n']} = {a['rate']} "
                 f"(95% {a['ci95'][0]}–{a['ci95'][1]})")
    lines.append(f"  판정 불가 {u['k']}/{u['n']} = {u['rate']} — 정답 분포: "
                 + ", ".join(f"{t} {n}" for t, n in r["undecided_truth"].items()))
    cu = r["column_unused"]
    lines.append(f"  점검을 하지 않은 날(column_unused): 검수한 쪽 {cu['pages']}개 중 맞음 {cu['agree']}")
    for p in cu["by_page"]:
        if not p["agree"]:
            lines.append(f"    {p['date']}: 기계 {'안 씀' if p['machine_unused'] else '씀'}, 정답 "
                         f"{'안 씀' if p['truth_unused'] else '씀'} (검수한 행 {p['rows']})")
    return "\n".join(lines)
