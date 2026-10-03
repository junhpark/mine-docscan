"""DB 요약 리포트. 사람이 보는 현황판이자 회귀 테스트가 비교하는 수치다.

여기의 값은 전부 DB 에서 다시 계산한다 — 실행 중에 센 값이 아니라 적재된 결과를 본다.
"""
from __future__ import annotations

import sqlite3


def _pairs(con: sqlite3.Connection, sql: str) -> dict:
    return {("unknown" if r[0] is None else str(r[0])): r[1] for r in con.execute(sql)}


def build_report(con: sqlite3.Connection) -> dict:
    one = lambda sql: con.execute(sql).fetchone()[0] or 0      # noqa: E731
    align = {}
    for r in con.execute("SELECT template_name, COUNT(*), SUM(align_ok), MIN(align_inliers), MAX(align_grid_err) "
                         "FROM doc_page WHERE align_ok IS NOT NULL GROUP BY 1 ORDER BY 1"):
        align[r[0]] = {"pages": r[1], "ok": r[2] or 0, "min_inliers": r[3],
                       "max_grid_err": None if r[4] is None else round(r[4], 2)}
    return {
        "documents": one("SELECT COUNT(*) FROM doc_document"),
        "documents_by_status": _pairs(con, "SELECT status, COUNT(*) FROM doc_document GROUP BY 1 ORDER BY 1"),
        "pages": one("SELECT COUNT(*) FROM doc_page"),
        "pages_by_form": _pairs(con, "SELECT template_name, COUNT(*) FROM doc_page GROUP BY 1 ORDER BY 1"),
        "pages_by_status": _pairs(con, "SELECT status, COUNT(*) FROM doc_page GROUP BY 1 ORDER BY 1"),
        "align": align,
        "fields": {
            "total": one("SELECT COUNT(*) FROM doc_field"),
            "with_value": one("SELECT COUNT(*) FROM doc_field WHERE has_value = 1"),
            "pending": one("SELECT COUNT(*) FROM doc_field WHERE review_status = 'pending'"),
        },
        "inspection": {
            "rows": one("SELECT COUNT(*) FROM insp_daily"),
            "auto": one("SELECT COUNT(*) FROM insp_daily WHERE review_status = 'auto'"),
            "abnormal_yes": one("SELECT COUNT(*) FROM insp_daily WHERE abnormal = 1"),
            "abnormal_no": one("SELECT COUNT(*) FROM insp_daily WHERE abnormal = 0"),
            "abnormal_undecided": one("SELECT COUNT(*) FROM insp_daily WHERE abnormal IS NULL"),
        },
        "haul": {
            "cells": one("SELECT COUNT(*) FROM prod_haul"),
            "filled": one("SELECT COUNT(*) FROM prod_haul WHERE has_value = 1"),
            "filled_by_role": _pairs(con, "SELECT source_role, COUNT(*) FROM prod_haul WHERE has_value = 1 "
                                          "GROUP BY 1 ORDER BY 1"),
            "with_trips": one("SELECT COUNT(*) FROM prod_haul WHERE trips IS NOT NULL"),
        },
        "xcheck_haul": _pairs(con, "SELECT status, COUNT(*) FROM xcheck_haul GROUP BY 1 ORDER BY 1"),
        # 일치율: 양쪽 다 횟수가 있는 칸 중 횟수가 같은 비율. 최종 값 기준과 기계 값 기준을 따로 (ADR 0007).
        # 정확도가 아니다 — 두 문서를 같은 방식으로 틀리게 읽으면 일치로 잡힌다. 분모(칸 수)를 같이 본다.
        "xcheck_agreement": {"final": _agreement(con, "log_trips", "matrix_trips"),
                             "raw": _agreement(con, "log_trips_raw", "matrix_trips_raw")},
        "reviews": {
            "fields": one("SELECT COUNT(DISTINCT field_id) FROM doc_review"),
            "fields_reviewed": one("SELECT COUNT(*) FROM doc_field WHERE review_status = 'reviewed'"),
        },
        "assignments": {
            "n": one("SELECT COUNT(*) FROM eq_assignment_obs"),
            "header_mismatch": one("SELECT COUNT(*) FROM eq_assignment_obs WHERE header_mismatch = 1"),
        },
        "equipment": one("SELECT COUNT(*) FROM eq_equipment"),
    }


def _agreement(con: sqlite3.Connection, log_col: str, matrix_col: str) -> dict:
    both, same = con.execute(f"SELECT COUNT(*), COALESCE(SUM({log_col} = {matrix_col}), 0) FROM xcheck_haul "
                             f"WHERE {log_col} IS NOT NULL AND {matrix_col} IS NOT NULL").fetchone()
    return {"both": both, "same": same, "rate": None if not both else round(same / both, 4)}


def by_month(con: sqlite3.Connection, min_margin: float = 1.5) -> list[dict]:
    """양식 × 월: 쪽 수, 적재, 정합 실패, 오류, 인라이어 최소, 괘선 오차 중앙값·최대, 분류 여유가 낮은 쪽 수.
    양식을 못 찾은 쪽(unknown)은 월별로 따로 나온다. 어느 달부터 수치가 나빠지면 그 달에 양식이 개정된 것이다."""
    groups: dict[tuple, dict] = {}
    for r in con.execute("SELECT template_name, substr(work_date, 1, 7), status, align_inliers, align_grid_err, classify_margin "
                         "FROM doc_page"):
        key = (r[0] or "unknown", r[1] or "unknown")
        g = groups.setdefault(key, {"template": key[0], "month": key[1], "pages": 0, "loaded": 0, "align_failed": 0,
                                    "error": 0, "classified_only": 0, "min_inliers": None, "grid_err": [],
                                    "low_margin": 0})
        g["pages"] += 1
        if r[2] in ("loaded", "align_failed", "error", "classified_only"):
            g[r[2]] += 1
        if r[3] is not None:
            g["min_inliers"] = r[3] if g["min_inliers"] is None else min(g["min_inliers"], r[3])
        if r[4] is not None:
            g["grid_err"].append(r[4])
        if r[5] is not None and r[5] < min_margin:
            g["low_margin"] += 1
    out = []
    for g in (groups[k] for k in sorted(groups, key=lambda k: (k[1], k[0]))):
        errs = sorted(g.pop("grid_err"))
        g["grid_err_median"] = None if not errs else round(errs[len(errs) // 2], 2)
        g["grid_err_max"] = None if not errs else round(errs[-1], 2)
        out.append(g)
    return out


def format_by_month(rows: list[dict]) -> str:
    head = f"{'월':<8} {'양식':<24} {'쪽':>5} {'적재':>5} {'정합실패':>8} {'오류':>4} {'인라이어최소':>10} {'괘선중앙':>8} {'괘선최대':>8} {'여유낮음':>8}"
    lines = [head]
    for g in rows:
        v = lambda x: "-" if x is None else str(x)        # noqa: E731 — 0.0 도 값이다
        lines.append(f"{g['month']:<8} {g['template']:<24} {g['pages']:>5} {g['loaded']:>5} {g['align_failed']:>8} "
                     f"{g['error']:>4} {v(g['min_inliers']):>10} {v(g['grid_err_median']):>8} "
                     f"{v(g['grid_err_max']):>8} {g['low_margin']:>8}")
    return "\n".join(lines)


def list_pages(con: sqlite3.Connection, status: str | None = None, template: str | None = None,
               low_margin: float | None = None) -> list[dict]:
    """쪽 목록: 출처(파일명#쪽), 날짜, 양식, 분류 여유, 인라이어, 괘선 오차, 상태, 오류."""
    sql = ("SELECT d.source_name || '#' || p.page_no AS source, p.page_id, p.document_id, p.page_no, p.work_date, "
           "p.template_name, p.classify_margin, p.align_inliers, p.align_grid_err, p.status, p.error, d.source_path, d.source_rel "
           "FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id WHERE 1=1")
    args: list = []
    if status:
        sql += " AND p.status = ?"
        args.append(status)
    if template:
        sql += " AND p.template_name = ?"
        args.append(template)
    if low_margin is not None:
        sql += " AND p.classify_margin IS NOT NULL AND p.classify_margin < ?"
        args.append(low_margin)
    sql += " ORDER BY p.work_date, d.source_name, p.page_no"
    return [dict(r) for r in con.execute(sql, args)]


def format_pages(rows: list[dict]) -> str:
    lines = [f"{'출처':<28} {'날짜':<10} {'양식':<24} {'여유':>5} {'인라이어':>7} {'괘선':>5} {'상태':<16} 오류"]
    for r in rows:
        m = "-" if r["classify_margin"] is None else f"{r['classify_margin']:.2f}"
        g = "-" if r["align_grid_err"] is None else f"{r['align_grid_err']:.1f}"
        lines.append(f"{r['source']:<28} {r['work_date'] or '-':<10} {r['template_name'] or '-':<24} {m:>5} "
                     f"{str(r['align_inliers'] or '-'):>7} {g:>5} {r['status']:<16} {r['error'] or ''}")
    return "\n".join(lines)


def xcheck_by_date(con: sqlite3.Connection) -> list[dict]:
    """날짜별 교차검증 결과 — 물질수지 관점의 '어디가 안 맞는가'를 날마다 보여준다."""
    out: dict[str, dict] = {}
    for r in con.execute("SELECT work_date, status, COUNT(*) FROM xcheck_haul GROUP BY 1, 2 ORDER BY 1, 2"):
        out.setdefault(r[0], {"work_date": r[0]})[r[1]] = r[2]
    return list(out.values())


def format_report(rep: dict, by_date: list[dict] | None = None) -> str:
    def kv(d: dict) -> str:
        return ", ".join(f"{k} {v}" for k, v in d.items()) or "-"

    lines = [
        f"문서 {rep['documents']}건 ({kv(rep['documents_by_status'])})",
        f"페이지 {rep['pages']}장 — 상태: {kv(rep['pages_by_status'])}",
        "양식별 페이지: " + kv(rep["pages_by_form"]),
        "정합:",
    ]
    for name, a in rep["align"].items():
        lines.append(f"  {name}: {a['ok']}/{a['pages']} 통과, 인라이어 최소 {a['min_inliers']}, "
                     f"괘선 오차 최대 {a['max_grid_err']} px")
    f, i, h = rep["fields"], rep["inspection"], rep["haul"]
    lines += [
        f"필드 {f['total']}개 (값 있음 {f['with_value']}, 검수 대기 {f['pending']})",
        f"점검 {i['rows']}행 — 자동 적재 {i['auto']}, 이상 유 {i['abnormal_yes']} / 무 {i['abnormal_no']} / "
        f"판정 불가 {i['abnormal_undecided']}",
        f"운반 셀 {h['cells']}개 — 값 있음 {h['filled']} ({kv(h['filled_by_role'])}), 횟수 인식 {h['with_trips']}",
        "교차검증(일보↔행렬): " + kv(rep["xcheck_haul"]),
        "횟수 일치율(양쪽 값 있는 칸): " + ", ".join(
            f"{k} {a['same']}/{a['both']}" + ("" if a["rate"] is None else f" = {a['rate']}")
            for k, a in rep["xcheck_agreement"].items()),
        f"검수: 필드 {rep['reviews']['fields']}개 검수 기록, reviewed 상태 {rep['reviews']['fields_reviewed']}개",
        f"배차 관측 {rep['assignments']['n']}건 — 인쇄된 머리글과 다름 {rep['assignments']['header_mismatch']}건",
        f"장비 마스터 {rep['equipment']}대",
    ]
    if by_date:
        lines.append("날짜별 교차검증:")
        for d in by_date:
            lines.append(f"  {d['work_date']}: " + kv({k: v for k, v in d.items() if k != 'work_date'}))
    return "\n".join(lines)
