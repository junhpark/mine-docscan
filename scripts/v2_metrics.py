"""확장성 표 (tasks/0009 4.7 다, 시험 성적서): 합성 묶음(--v2-forms)을 보통 글씨와 거친 글씨(--rough)로 만들어 양식마다
쪽 수, 분류, 정합 통과, 괘선 오차의 중앙, 칸 수, 인식기에 보낸 칸의 oracle CER, 값 유무의 정밀도·재현율(null — 정답의 잉크 유무와)을 잰다.

    python scripts/v2_metrics.py --out docs/test-report/v2-<날짜>.json [--days 3]

합성 데이터의 수치다 — 확장성(템플릿만으로 새 양식이 들어간다)의 증거이지 인식률이 아니다 (CLAUDE.md "하지 말 것").
값 유무: 손글씨 칸(handwritten_*)에서 정답이 있으면 잉크 있음. 서명·✓ 칸은 보지 않는다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path


def measure(days: int, rough: bool, root: Path) -> dict:
    from minedocscan.config import Settings
    from minedocscan.evaluate.metrics import corpus_cer
    from minedocscan.forms.formats import try_normalize
    from minedocscan.pipeline import Pipeline
    from minedocscan.recognize import OracleRecognizer, load_answers_json
    from minedocscan.tools.synth import generate

    syn = generate(root / "synth", days=days, seed=0, v2_forms=True, usage_logs=True, print_layers=True, rough=rough)
    answers = load_answers_json(syn.answers_path)
    expected = syn.truth["expected"]["pages_by_form"]
    out: dict = {}
    runs = {}
    for name, rec in (("oracle", OracleRecognizer(answers)), ("null", None)):
        st = Settings(site=syn.site, archive_root=syn.scans, work_root=root / name, reviews=root / name / "r.jsonl")
        p = Pipeline(st, recognizer=rec)
        p.run([syn.scans])
        runs[name] = p
    con = runs["oracle"].con
    pages = defaultdict(list)
    for r in con.execute("SELECT template_name, status, align_grid_err FROM doc_page"):
        pages[r[0]].append((r[1], r[2]))
    tables = {(o, t, rg) for (o, t, rg, *_r) in answers}
    q = ("SELECT d.source_name, p.page_no, p.work_date, p.template_name, f.region, f.field_name, f.row_key, f.format, f.value_raw, "
         "f.has_value_raw FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
         "WHERE f.kind LIKE 'handwritten%' AND p.status = 'loaded'")

    def truth_of(r):
        src = f"{r[0]}#{r[1]}"
        origin = src if (src, r[3], r[4]) in tables else r[2]
        if (origin, r[3], r[4]) not in tables:
            return None
        return answers.get((origin, r[3], r[4], r[5], r[6] or ""), "")

    cer_pairs, cells = defaultdict(list), defaultdict(int)
    for r in con.execute(q):
        cells[r[3]] += 1
        t = truth_of(r)
        if t is None or r[7] not in (None, "integer") or not r[9]:      # 인식기에 보낸 칸 (형식 없음·integer, 값 있음)
            continue
        cer_pairs[r[3]].append((try_normalize(r[7], r[8] or "") or "", try_normalize(r[7], t) or ""))
    presence = defaultdict(lambda: [0, 0, 0, 0])                        # tp fp fn tn
    for r in runs["null"].con.execute(q):
        t = truth_of(r)
        if t is None:
            continue
        k = presence[r[3]]
        k[(0 if r[9] else 2) if t.strip() else (1 if r[9] else 3)] += 1
    for form in sorted(set(expected) | set(pages)):
        st = pages.get(form, [])
        errs = [e for s, e in st if s == "loaded" and e is not None]
        tp, fp, fn, tn = presence[form]
        out[form] = {"pages": expected.get(form, 0), "classified": len(st), "loaded": sum(s == "loaded" for s, _e in st),
                     "grid_err_median": round(statistics.median(errs), 2) if errs else None, "cells": cells.get(form, 0),
                     "sent_cells": len(cer_pairs.get(form, [])),
                     "oracle_cer": round(corpus_cer(cer_pairs[form]), 4) if cer_pairs.get(form) else None,
                     "presence_precision": round(tp / (tp + fp), 4) if tp + fp else None,
                     "presence_recall": round(tp / (tp + fn), 4) if tp + fn else None, "presence_n": tp + fp + fn + tn}
    for p in runs.values():
        p.con.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args(argv)
    result = {"days": a.days, "seed": 0}
    for label, rough in (("normal", False), ("rough", True)):
        with tempfile.TemporaryDirectory() as tmp:
            result[label] = measure(a.days, rough, Path(tmp))
        print(label, json.dumps(result[label], ensure_ascii=False), flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
