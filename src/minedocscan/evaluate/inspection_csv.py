"""점검표 정답 CSV(기존 연구팀 형식) 로더와 평가.

형식: 파일명 YYMMDD.csv, cp949, 머리 3줄(제목·날짜·열 이름) 뒤에 양식의 행 순서대로
      `구분,형식,등록번호,점검내역`. 구분이 비어 있으면 위 행의 구분을 잇는다.
      점검내역의 '?' 는 원본의 가운뎃점이 인코딩 과정에서 깨진 것이므로 공백으로 읽는다.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

from ..forms.template import Template, row_key
from .metrics import auto_rate, corpus_cer, field_accuracy


def load_gt_csv(path: str | Path) -> tuple[str, list[str]]:
    """(YYYY-MM-DD, 행 순서대로의 점검내역) 을 돌려준다."""
    p = Path(path)
    raw = p.read_bytes()
    for enc in ("cp949", "utf-8-sig"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"인코딩을 알 수 없습니다: {p}")
    stem = p.stem
    date = f"20{stem[:2]}-{stem[2:4]}-{stem[4:6]}"
    remarks = []
    for r in list(csv.reader(io.StringIO(text)))[3:]:
        r = (r + ["", "", "", ""])[:4]
        remarks.append(" ".join(r[3].replace("?", " ").split()))
    return date, remarks


def load_answers(gt_dir: str | Path, tpl: Template) -> dict[tuple[str, str, str, str, str], str]:
    """정답 폴더 → OracleRecognizer 가 받는 answers. 행 순서가 템플릿과 같다는 전제."""
    opt = tpl.handler_options
    region = opt.get("region") or tpl.regions[0]["name"]
    text_col = opt.get("text_column", "remark")
    rows = tpl.region(region)["rows"]
    answers = {}
    for f in sorted(Path(gt_dir).glob("*.csv")):
        date, remarks = load_gt_csv(f)
        if len(remarks) != len(rows):
            raise ValueError(f"{f.name}: 행 수 {len(remarks)} ≠ 템플릿 행 수 {len(rows)}")
        for r, remark in zip(rows, remarks, strict=True):
            answers[(date, tpl.name, region, text_col, row_key(r))] = remark
    return answers


def evaluate_inspection(con: sqlite3.Connection, answers: dict, tpl: Template) -> dict:
    """insp_daily 의 점검내역을 정답과 비교한다."""
    opt = tpl.handler_options
    region = opt.get("region") or tpl.regions[0]["name"]
    text_col = opt.get("text_column", "remark")
    pairs, statuses = [], []
    for row in con.execute("SELECT d.inspection_date, e.equipment_key, d.remark, d.review_status "
                           "FROM insp_daily d JOIN eq_equipment e ON d.equipment_id = e.equipment_id"):
        key = (row[0], tpl.name, region, text_col, row[1])
        if key in answers:
            pairs.append((row[2] or "", answers[key]))
            statuses.append(row[3])
    return {"rows": len(pairs), "remark_corpus_cer": round(corpus_cer(pairs), 4),
            "remark_field_accuracy": round(field_accuracy(pairs), 4), "auto_rate": round(auto_rate(statuses), 4)}
