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
