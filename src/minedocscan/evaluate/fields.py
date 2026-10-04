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

자동 적재 오류율(auto_error): 인식기가 자동 적재한 칸(값이든 빈 칸이든 — doc_field.status_raw = auto, backend 가
ink·template 이 아닌 것) 중 기계의 답이 정답과 다른 비율. 분자·분모와 윌슨 95 % 구간을 같이 낸다 (tasks/0003 4.6).
기계의 답 = value_raw (기계가 값 없음으로 정했으면 빈 칸). 검수 여부와 상관없이 기계의 판단을 본다 — target 과 무관하다.
잉크가 없어 빈 칸으로 확정한 칸(backend ink)은 따로 센다 (ink_auto).
"""
from __future__ import annotations

import sqlite3

from .metrics import auto_rate, corpus_cer, field_accuracy, normalize
from .stats import wilson

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
            f"f.kind, {col}, f.review_status, f.status_raw, f.backend, f.has_value_raw, f.value_raw "
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
        g = groups.setdefault(f"{template}/{r[7]}", {"pairs": [], "statuses": [], "machine": []})
        truth = answers.get(key, "")
        g["pairs"].append((r[8] or "", truth))
        g["statuses"].append(r[10] or r[9])                     # 자동 적재율은 기계가 정한 상태(status_raw)로 — 검수와 무관
        machine = (r[13] or "") if r[12] else ""                 # 기계가 값 없음으로 정했으면 빈 칸
        g["machine"].append((r[10], r[11], machine, truth))

    def summarize(pairs, statuses, machine) -> dict:
        valued = [p for p in pairs if normalize(p[1])]
        empty = [p for p in pairs if not normalize(p[1])]
        return {"n": len(pairs), "cer": round(corpus_cer(pairs), 4),
                "field_accuracy": round(field_accuracy(pairs), 4), "auto_rate": round(auto_rate(statuses), 4),
                "n_value": len(valued), "accuracy_value": None if not valued else round(field_accuracy(valued), 4),
                "n_empty": len(empty), "accuracy_empty": None if not empty else round(field_accuracy(empty), 4),
                "auto_error": auto_error(machine)}

    out = summarize([p for g in groups.values() for p in g["pairs"]],
                    [s for g in groups.values() for s in g["statuses"]],
                    [m for g in groups.values() for m in g["machine"]])
    out["target"] = target
    out["split"] = split
    out["answers_not_in_db"] = len(set(answers) - seen)      # 페이지가 적재되지 않아 비교하지 못한 정답
    out["by_field_kind"] = {k: summarize(g["pairs"], g["statuses"], g["machine"]) for k, g in sorted(groups.items())}
    return out


def auto_error(machine: list[tuple]) -> dict:
    """machine: [(status_raw, backend, 기계의 답, 정답)]. 인식기가 자동 적재한 칸 중 정답과 다른 것."""
    auto = [(a, t) for st, b, a, t in machine if st == "auto" and b not in ("ink", "template")]
    wrong = sum(normalize(a) != normalize(t) for a, t in auto)
    lo, hi = wilson(wrong, len(auto))
    return {"auto": len(auto), "auto_value": sum(bool(normalize(a)) for a, _t in auto),
            "auto_empty": sum(not normalize(a) for a, _t in auto), "wrong": wrong,
            "rate": None if not auto else round(wrong / len(auto), 4), "ci95": [round(lo, 4), round(hi, 4)],
            "ink_auto": sum(st == "auto" and b == "ink" for st, b, _a, _t in machine)}


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
