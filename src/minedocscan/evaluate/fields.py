"""필드 단위 평가: doc_field 의 값을 정답과 비교한다. 양식에 상관없이 쓴다.

정답 키는 오라클 백엔드와 같다 — (출처, template, region, field_name, row_key),
출처는 "<파일명>#<페이지>" 또는 "YYYY-MM-DD" (recognize/builtin.py).

어떤 표에 정답이 하나라도 있으면 그 표의 수기 셀 전부를 평가한다. 정답에 없는 셀은 빈 칸이 정답이다.
(빈 칸에 값을 만들어 내는 오류도 잡아야 하므로.) 정답이 전혀 없는 표는 평가하지 않는다.
단, 표본만 검수해서 만든 정답(review export-answers)은 그 표의 나머지 셀이 빈 칸이라는 뜻이 아니므로
only_listed=True 로 정답에 있는 셀만 평가한다.

target:  final = value_final (교정·검수 후 최종값),  raw = value_raw (기계가 읽은 값).
검수값과 비교할 때는 raw 를 쓴다 — final 은 검수값 자신이라 언제나 맞는다.

정답이 빈 칸인 셀과 값이 있는 셀의 정확도를 따로 낸다. 빈 칸이 대부분이라 합치면 인식기의 성적이 가려진다.
"""
from __future__ import annotations

import sqlite3

from .metrics import auto_rate, corpus_cer, field_accuracy, normalize

TARGETS = ("final", "raw")


def evaluate_fields(con: sqlite3.Connection, answers: dict, target: str = "final", only_listed: bool = False,
                    split: str = "all", site=None) -> dict:
    """split: all | test | train — 쪽의 날짜가 그 분할인 셀만 센다 (site 의 소금값으로 정한다, ADR 0009)."""
    if target not in TARGETS:
        raise ValueError(f"target 은 {TARGETS} 중 하나: {target}")
    if split != "all" and site is None:
        raise ValueError("split 에는 사이트 팩이 필요합니다")
    col = "f.value_final" if target == "final" else "f.value_raw"
    tables = {(k[0], k[1], k[2]) for k in answers}          # (출처, template, region) 에 정답이 있는가
    groups: dict[str, dict] = {}
    seen: set = set()
    for r in con.execute(
            "SELECT d.source_name, p.page_no, p.work_date, p.template_name, f.region, f.field_name, f.row_key, "
            f"f.kind, {col}, f.review_status "
            "FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
            "JOIN doc_document d ON p.document_id = d.document_id WHERE f.kind LIKE 'handwritten%'"):
        source, work_date, template, region = f"{r[0]}#{r[1]}", r[2], r[3], r[4]
        if split != "all" and site.split_of(work_date) != split:
            continue
        origin = source if (source, template, region) in tables else work_date
        if (origin, template, region) not in tables:
            continue
        key = (origin, template, region, r[5], r[6] or "")
        if key in answers:
            seen.add(key)
        elif only_listed:
            continue
        g = groups.setdefault(f"{template}/{r[7]}", {"pairs": [], "statuses": []})
        g["pairs"].append((r[8] or "", answers.get(key, "")))
        g["statuses"].append(r[9])

    def summarize(pairs, statuses) -> dict:
        valued = [p for p in pairs if normalize(p[1])]
        empty = [p for p in pairs if not normalize(p[1])]
        return {"n": len(pairs), "cer": round(corpus_cer(pairs), 4),
                "field_accuracy": round(field_accuracy(pairs), 4), "auto_rate": round(auto_rate(statuses), 4),
                "n_value": len(valued), "accuracy_value": None if not valued else round(field_accuracy(valued), 4),
                "n_empty": len(empty), "accuracy_empty": None if not empty else round(field_accuracy(empty), 4)}

    out = summarize([p for g in groups.values() for p in g["pairs"]],
                    [s for g in groups.values() for s in g["statuses"]])
    out["target"] = target
    out["split"] = split
    out["answers_not_in_db"] = len(set(answers) - seen)      # 페이지가 적재되지 않아 비교하지 못한 정답
    out["by_field_kind"] = {k: summarize(g["pairs"], g["statuses"]) for k, g in sorted(groups.items())}
    return out


def evaluate_presence(con: sqlite3.Connection) -> dict:
    """값 유무 판단의 성적: 검수가 있는 셀에서 기계의 has_value_raw 대 검수의 value/empty → 정밀도·재현율.
    (illegible 은 뺀다. 검수 기록은 DB 의 doc_review 에서 읽는다 — 파이프라인·검수 서버가 파일을 읽어 들여 둔다.)"""
    from ..review.store import effective

    tp = fp = fn = tn = 0
    raw = dict(con.execute("SELECT field_id, has_value_raw FROM doc_field WHERE kind LIKE 'handwritten%'"))
    for fid, rv in effective(con).items():
        if rv.verdict == "illegible" or fid not in raw:
            continue
        pred, truth = raw[fid] == 1, rv.verdict == "value"
        if pred and truth:
            tp += 1
        elif pred:
            fp += 1
        elif truth:
            fn += 1
        else:
            tn += 1
    n = tp + fp + fn + tn
    return {"n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": None if not (tp + fp) else round(tp / (tp + fp), 4),
            "recall": None if not (tp + fn) else round(tp / (tp + fn), 4)}
