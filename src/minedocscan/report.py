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
        "warnings": {"n": one("SELECT COUNT(*) FROM doc_document WHERE warning IS NOT NULL"),
                     "documents": [r[0] for r in con.execute(
                         "SELECT source_name FROM doc_document WHERE warning IS NOT NULL ORDER BY source_name")]},
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
        # 수기 칸을 값으로 만든 주체별: 칸 수, 기계가 자동 적재한 수(status_raw), 지금 검수 대기·검수된 수
        "fields_by_backend": _by_backend(con),
        # 쪽 메타 (tasks/0004): 키마다 출처별·대조 결과별 쪽 수, 기계의 상태별 수. 일보 중 자리가 정해진 쪽과 그 출처
        "page_meta": page_meta_summary(con),
        "log_slots": log_slots(con),
        # 장비 가동 일보 (tasks/0005): 양식별 쪽 수, 계기 칸의 종류별, 가동 시간의 근거별, 장비 ID 가 정해진 쪽, 작업량 칸
        "usage": usage_summary(con),
        # 가동 일보의 검산 (tasks/0005 4.4): 종류(total | subtotal | continuity)별 결과별 수
        "xcheck_usage": xcheck_usage_summary(con),
    }


def xcheck_usage_summary(con: sqlite3.Connection) -> dict:
    out: dict = {}
    for k, res, n in con.execute("SELECT check_kind, result, COUNT(*) FROM xcheck_usage GROUP BY 1, 2 ORDER BY 1, 2"):
        out.setdefault(k, {})[res] = n
    return out


def xcheck_usage_by_date(con: sqlite3.Connection) -> list[dict]:
    """날짜별 가동 일보 검산: {work_date, "<종류>.<결과>": 수 …}. 계기가 어디서 이어지지 않는가를 날마다."""
    out: dict[str, dict] = {}
    for d, k, res, n in con.execute("SELECT work_date, check_kind, result, COUNT(*) FROM xcheck_usage GROUP BY 1, 2, 3 "
                                    "ORDER BY 1, 2, 3"):
        out.setdefault(d or "unknown", {"work_date": d or "unknown"})[f"{k}.{res}"] = n
    return list(out.values())


def usage_summary(con: sqlite3.Connection) -> dict:
    """가동 기록: 수만 (값·이름 없이). reading = 계기 칸 meter(계기 값) | clock(시각) | empty(빈 칸) | pending(검수 대기) |
    mixed | none(계기 표 없음), hours_basis = meter | total | clock | shifts | none(가동 시간 NULL)."""
    one = lambda sql: con.execute(sql).fetchone()[0] or 0      # noqa: E731
    return {
        "pages": one("SELECT COUNT(*) FROM eq_usage_daily"),
        "by_form": _pairs(con, "SELECT source_form, COUNT(*) FROM eq_usage_daily GROUP BY 1 ORDER BY 1"),
        "reading": _pairs(con, "SELECT reading_kind, COUNT(*) FROM eq_usage_daily GROUP BY 1 ORDER BY 1"),
        "hours_basis": {("none" if k == "unknown" else k): v for k, v in _pairs(
            con, "SELECT hours_basis, COUNT(*) FROM eq_usage_daily GROUP BY 1 ORDER BY 1").items()},
        "pending": one("SELECT COUNT(*) FROM eq_usage_daily WHERE review_status = 'pending'"),
        "with_equipment": one("SELECT COUNT(*) FROM eq_usage_daily WHERE equipment IS NOT NULL"),
        "with_equipment_id": one("SELECT COUNT(*) FROM eq_usage_daily WHERE equipment_id IS NOT NULL"),
        "tally": {"cells": one("SELECT COUNT(*) FROM prod_tally"),
                  "filled": one("SELECT COUNT(*) FROM prod_tally WHERE has_value = 1"),
                  "with_count": one("SELECT COUNT(*) FROM prod_tally WHERE count IS NOT NULL")},
    }


def stale_equipment_ids(con: sqlite3.Connection, site) -> dict:
    """[equipment.aliases] 를 고친 뒤 다시 돌리지 않은 행: eq_usage_daily·prod_tally 에서 equipment_id 가 지금 대응표로 정한 값
    (site.equipment_id_of(equipment))과 다른 행의 수, 그 행이 있는 쪽·문서의 수. 수만 (이름·장비 키 없이).
    build_report 에 넣지 않는다 — 사이트 팩에 따라 달라지는 수라 regress 의 기준과 비교하지 않는다."""
    out = {"eq_usage_daily": 0, "prod_tally": 0}
    pages: set[str] = set()
    for table, key in (("eq_usage_daily", "page_id"), ("prod_tally", "tally_id")):
        for page_id, equipment, eid in con.execute(f"SELECT page_id, equipment, equipment_id FROM {table} ORDER BY {key}"):
            if eid != site.equipment_id_of(equipment):         # None 끼리는 같다
                out[table] += 1
                pages.add(page_id)
    out["pages"] = len(pages)
    out["documents"] = len({d for p, d in con.execute("SELECT page_id, document_id FROM doc_page") if p in pages})
    return out


def format_stale_equipment_ids(st: dict) -> str:
    """한 줄 (없으면 빈 문자열)."""
    if not (st["eq_usage_daily"] or st["prod_tally"]):
        return ""
    return (f"장비 ID 가 지금의 대응표([equipment.aliases])와 다른 행: 가동 기록 {st['eq_usage_daily']}행, 작업량 {st['prod_tally']}행 "
            f"(쪽 {st['pages']}, 문서 {st['documents']}건) — 대응표를 고친 뒤 다시 돌리지 않았습니다. "
            "run --fresh 또는 그 문서를 다시 돌리세요")


def page_meta_summary(con: sqlite3.Connection) -> dict:
    """키마다: 쪽 수, 출처별(review | label | filename | machine | none), 대조 결과별, 기계의 상태별 (읽지 않았으면 빠진다)."""
    out: dict = {}
    for k, src, chk, ms, n in con.execute(
            "SELECT meta_key, COALESCE(source, 'none'), check_result, machine_status, COUNT(*) FROM doc_page_meta "
            "GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4"):
        d = out.setdefault(k, {"pages": 0, "by_source": {}, "by_check": {}, "machine": {}})
        d["pages"] += n
        d["by_source"][src] = d["by_source"].get(src, 0) + n
        d["by_check"][chk] = d["by_check"].get(chk, 0) + n
        if ms is not None:
            d["machine"][ms] = d["machine"].get(ms, 0) + n
    return out


def log_slots(con: sqlite3.Connection) -> dict:
    """일보(prod_haul 의 log) 쪽 중 행렬의 자리가 정해진 쪽의 수와, 자리를 정한 키(작성자·차량번호)의 출처별 수."""
    pages = con.execute("SELECT page_id, MAX(work_date), MAX(slot) FROM prod_haul WHERE source_role = 'log' GROUP BY 1").fetchall()
    how = {(r[0], r[1]): r[2] for r in con.execute("SELECT work_date, slot, matched_by FROM eq_assignment_obs")}
    src = {(r[0], r[1]): r[2] for r in con.execute(
        "SELECT page_id, meta_key, COALESCE(source, 'none') FROM doc_page_meta WHERE meta_key IN ('operator', 'vehicle_no')")}
    by_source: dict[str, int] = {}
    resolved = 0
    for pid, date, slot in pages:
        if slot is None:
            continue
        resolved += 1
        key = "operator" if how.get((date, slot)) == "operator" else "vehicle_no"
        s = src.get((pid, key), "none")
        by_source[s] = by_source.get(s, 0) + 1
    return {"log_pages": len(pages), "resolved": resolved, "by_source": dict(sorted(by_source.items()))}


def _by_backend(con: sqlite3.Connection) -> dict:
    return {("unknown" if r[0] is None else r[0]): {"fields": r[1], "auto": r[2] or 0, "pending": r[3] or 0,
                                                     "reviewed": r[4] or 0}
            for r in con.execute("SELECT backend, COUNT(*), SUM(status_raw = 'auto'), SUM(review_status = 'pending'), "
                                 "SUM(review_status = 'reviewed') FROM doc_field WHERE kind LIKE 'handwritten%' "
                                 "GROUP BY 1 ORDER BY 1")}


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


def meta_mismatch_pages(con: sqlite3.Connection, meta_key: str | None = None) -> list[dict]:
    """기계가 읽은 값이 사람·파일명의 값과 다른 쪽 (doc_page_meta.check_result = mismatch). 값은 내지 않는다 — 화면에서 본다."""
    sql = ("SELECT d.source_name || '#' || p.page_no AS source, p.page_id, p.work_date, p.template_name, m.meta_key, "
           "m.source AS value_source, m.machine_status, m.machine_confidence FROM doc_page_meta m "
           "JOIN doc_page p ON m.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
           "WHERE m.check_result = 'mismatch'")
    args: list = []
    if meta_key:
        sql += " AND m.meta_key = ?"
        args.append(meta_key)
    sql += " ORDER BY p.work_date, d.source_name, p.page_no, m.meta_key"
    return [dict(r) for r in con.execute(sql, args)]


def format_meta_mismatch(rows: list[dict]) -> str:
    lines = [f"{'출처':<28} {'날짜':<10} {'양식':<24} {'키':<11} {'값의 출처':<9} 기계"]
    for r in rows:
        conf = "-" if r["machine_confidence"] is None else f"{r['machine_confidence']:.2f}"
        lines.append(f"{r['source']:<28} {r['work_date'] or '-':<10} {r['template_name'] or '-':<24} {r['meta_key']:<11} "
                     f"{r['value_source'] or '-':<9} {r['machine_status']} ({conf})")
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
        f"문서 {rep['documents']}건 ({kv(rep['documents_by_status'])})"
        + (f" — 경고 {rep['warnings']['n']}건: {', '.join(rep['warnings']['documents'])}" if rep["warnings"]["n"] else ""),
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
        "수기 칸 백엔드별: " + (", ".join(f"{b} {v['fields']} (자동 {v['auto']}, 대기 {v['pending']}, 검수됨 {v['reviewed']})"
                                       for b, v in rep["fields_by_backend"].items()) or "-"),
        f"점검 {i['rows']}행 — 자동 적재 {i['auto']}, 이상 유 {i['abnormal_yes']} / 무 {i['abnormal_no']} / "
        f"판정 불가 {i['abnormal_undecided']}",
        f"운반 셀 {h['cells']}개 — 값 있음 {h['filled']} ({kv(h['filled_by_role'])}), 횟수 인식 {h['with_trips']}",
        "교차검증(일보↔행렬): " + kv(rep["xcheck_haul"]),
        "횟수 일치율(양쪽 값 있는 칸): " + ", ".join(
            f"{k} {a['same']}/{a['both']}" + ("" if a["rate"] is None else f" = {a['rate']}")
            for k, a in rep["xcheck_agreement"].items()),
        f"검수: 필드 {rep['reviews']['fields']}개 검수 기록, reviewed 상태 {rep['reviews']['fields_reviewed']}개",
        f"배차 관측 {rep['assignments']['n']}건 — 인쇄된 머리글과 다름 {rep['assignments']['header_mismatch']}건",
        f"일보 {rep['log_slots']['log_pages']}쪽 중 자리가 정해진 쪽 {rep['log_slots']['resolved']} (정한 키의 출처: "
        f"{kv(rep['log_slots']['by_source'])})",
        f"장비 마스터 {rep['equipment']}대",
    ]
    for k, d in rep["page_meta"].items():
        if k == "date" and not d["machine"]:
            continue
        lines.append(f"쪽 메타 {k}: {d['pages']}쪽 — 출처 {kv(d['by_source'])}; 대조 {kv(d['by_check'])}"
                     + (f"; 기계 {kv(d['machine'])}" if d["machine"] else ""))
    u = rep.get("usage") or {}
    if u.get("pages"):
        lines += [
            f"가동 기록 {u['pages']}쪽 ({kv(u['by_form'])}) — 계기 칸: {kv(u['reading'])}; 검수 대기 {u['pending']}",
            f"  가동 시간의 근거: {kv(u['hours_basis'])}; 장비명 있음 {u['with_equipment']}, 장비 ID 정해짐 {u['with_equipment_id']}",
            f"  작업량 칸 {u['tally']['cells']}개 — 값 있음 {u['tally']['filled']}, 수 {u['tally']['with_count']}",
        ]
        names = {"total": "총 = 종료 − 시작", "subtotal": "소계 = 합", "continuity": "계기의 연속성"}
        for k, d in (rep.get("xcheck_usage") or {}).items():
            lines.append(f"  검산 {names.get(k, k)}: {kv(d)}")
    if by_date:
        lines.append("날짜별 교차검증:")
        for d in by_date:
            lines.append(f"  {d['work_date']}: " + kv({k: v for k, v in d.items() if k != 'work_date'}))
    return "\n".join(lines)
