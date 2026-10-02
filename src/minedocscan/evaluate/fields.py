"""필드 단위 평가: doc_field 의 최종값을 정답과 비교한다. 양식에 상관없이 쓴다.

정답 키는 오라클 백엔드와 같다 — (출처, template, region, field_name, row_key),
출처는 "<파일명>#<페이지>" 또는 "YYYY-MM-DD" (recognize/builtin.py).

어떤 표에 정답이 하나라도 있으면 그 표의 수기 셀 전부를 평가한다. 정답에 없는 셀은 빈 칸이 정답이다.
(빈 칸에 값을 만들어 내는 오류도 잡아야 하므로.) 정답이 전혀 없는 표는 평가하지 않는다.
"""
from __future__ import annotations

import sqlite3

from .metrics import auto_rate, corpus_cer, field_accuracy


def evaluate_fields(con: sqlite3.Connection, answers: dict) -> dict:
    tables = {(k[0], k[1], k[2]) for k in answers}          # (출처, template, region) 에 정답이 있는가
    groups: dict[str, dict] = {}
    seen: set = set()
    for r in con.execute(
            "SELECT d.source_name, p.page_no, p.work_date, p.template_name, f.region, f.field_name, f.row_key, "
            "f.kind, f.value_final, f.review_status "
            "FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
            "JOIN doc_document d ON p.document_id = d.document_id WHERE f.kind LIKE 'handwritten%'"):
        source, work_date, template, region = f"{r[0]}#{r[1]}", r[2], r[3], r[4]
        origin = source if (source, template, region) in tables else work_date
        if (origin, template, region) not in tables:
            continue
        key = (origin, template, region, r[5], r[6] or "")
        if key in answers:
            seen.add(key)
        g = groups.setdefault(f"{template}/{r[7]}", {"pairs": [], "statuses": []})
        g["pairs"].append((r[8] or "", answers.get(key, "")))
        g["statuses"].append(r[9])

    def summarize(pairs, statuses) -> dict:
        return {"n": len(pairs), "cer": round(corpus_cer(pairs), 4),
                "field_accuracy": round(field_accuracy(pairs), 4), "auto_rate": round(auto_rate(statuses), 4)}

    out = summarize([p for g in groups.values() for p in g["pairs"]],
                    [s for g in groups.values() for s in g["statuses"]])
    out["answers_not_in_db"] = len(set(answers) - seen)      # 페이지가 적재되지 않아 비교하지 못한 정답
    out["by_field_kind"] = {k: summarize(g["pairs"], g["statuses"]) for k, g in sorted(groups.items())}
    return out
