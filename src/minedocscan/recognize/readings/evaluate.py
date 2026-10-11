"""소수·시각 모델의 크롭 단위 평가 — 파이프라인을 돌리지 않고 export-crops 의 폴더에서 바로 (tasks/0011 4.6). torch 없이 돈다.

  minedocscan recognizer eval --crops DIR --model <readings 모델> [--split val|test|train] [--errors [DIR]]

분할: 숫자 모델과 같다 (val = 카드의 검증 규칙이 고르는 train 날짜, train = 그 나머지, test = test 로 내보낸 폴더 — 마지막에 한 번).
--cv 로 기준을 정한 모델의 val 은 학습 때 묶음마다의 모델로 읽은 기록(cv-reads.jsonl)으로 낸다 — 최종 모델은 그 날짜로 학습했다.
낸 것: 형식마다 값 정확도(formats.same_value)·빈 칸·거절, 자동 적재율과 오류(분자·분모·윌슨 구간), 점으로 쓴 시각으로 읽은 칸의 수,
임계값별 표. --errors: 틀린 칸을 한 장에 — 모델이 본 글씨 띠(파란 상자)와 칸(빨간 상자)을 같이 그린다. 현장 글씨가 들어 있으므로 git
작업 트리 안이면 거절한다. 출력은 집계뿐이다 — 칸마다의 값·field_id 는 찍지 않는다 (7절).
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from ...imaging.io import imwrite
from ...review.export import inside_git_tree
from ..digits import data as digits_data
from ..digits.evaluate import MAX_TILES, SPLITS, EvalError
from . import calib, data
from .backend import ReadingsRecognizer
from .model import REJECT, decode, preprocess
from .train import CV_READS, load_cv_reads


def select(crops_dir: str | Path, card: dict, split: str | None) -> tuple[list[data.Sample], data.Crops]:
    """분할의 칸. split=None 이면 폴더의 모든 형식 칸 (시험용)."""
    if split is not None and split not in SPLITS:
        raise EvalError(f"--split 은 {' | '.join(SPLITS)}: {split}")
    try:
        crops = data.read_crops(crops_dir, allow_test=True, only_split="test" if split == "test" else
                                ("train" if split in ("val", "train") else None))
    except digits_data.CropsError as e:
        raise EvalError(str(e)) from e
    if split == "test" and not crops.samples:
        raise EvalError("이 폴더에는 test 로 내보낸 형식 칸이 없습니다 — `review export-crops --split test` 로 내보낸 폴더를 주세요")
    if split in ("val", "train"):
        rule = card["data"]["val_rule"]
        tr, va = digits_data.split_val(crops.samples, rule["salt"], float(rule["share"]))
        chosen = va if split == "val" else tr
        if not chosen:
            raise EvalError(f"이 폴더에는 {split} 날짜의 형식 칸이 없습니다 (모델 카드의 규칙: 소금값 {rule['salt']!r}, 비율 "
                            f"{rule['share']}). 이 모델은 학습 때 {card['validation']['source']} 칸으로 검증했다")
        return chosen, crops
    return crops.samples, crops


def evaluate(crops_dir: str | Path, model_dir: str | Path, split: str | None = "val", threshold: float | None = None,
             errors: str | Path | None = None, allow_in_repo: bool = False) -> dict:
    """크롭 폴더를 모델로 읽어 성적을 낸다. errors: 틀린 칸 모아 보기를 쓸 폴더 (None 이면 쓰지 않는다)."""
    if errors is not None and inside_git_tree(errors) and not allow_in_repo:
        raise EvalError(f"{errors} 은 git 작업 트리 안입니다. 틀린 칸 모아 보기에는 현장의 글씨가 들어 있으므로 저장소 밖에 쓰세요")
    rec = ReadingsRecognizer(model_dir, threshold=threshold, threshold_source="config")
    card, t = rec.card, rec.threshold
    thr = None if not math.isfinite(t) else t
    base = {"model": card["name"], "split": split or "all", "spec": rec.spec.describe(), "threshold": thr,
            "threshold_source": rec.threshold_source, "errors_image": None}
    if split == "val" and card["validation"].get("method") == "cv":         # 묶음마다의 모델로 읽은 기록 (값 없이)
        rows = load_cv_reads(Path(model_dir) / CV_READS)
        return base | {"cells": len(rows), "dates": None, "source": f"{CV_READS} (학습 때 묶음마다의 모델로 읽은 것)",
                       "score": calib.score(rows, thr), "thresholds": calib.threshold_table(rows),
                       "skipped": {}, "errors": sum(not r["correct"] for r in rows), "predictions": []}
    samples, crops = select(crops_dir, card, split)
    n_no_ink = sum(not s.inked for s in samples)
    samples = [s for s in samples if s.inked]               # 인식기에 가는 칸만 (4.6 의 inked)
    if not samples:
        raise EvalError(f"평가할 칸이 없습니다 (잉크가 없던 칸 {n_no_ink}개만 있다)")
    if crops.spec is not None and crops.spec != rec.spec:
        raise EvalError(f"크롭의 규격({crops.spec.describe()})이 모델의 규격({rec.spec.describe()})과 다릅니다 — "
                        "모델과 같은 규격으로 내보낸 폴더를 주세요 (tasks/0003 4.1)")
    reads, shown = [], []
    for s in samples:
        img = s.image()
        x, b = preprocess(img, s.cell, s.scale)
        reads.append(calib.Read(s.fmt, s.value, decode(rec.net.logits(x), s.fmt, rec.temperature)))
        shown.append((img, s.cell, b.rect))
    rows = calib.flags(reads)
    out = base | {"cells": len(samples), "dates": len({s.work_date for s in samples if s.work_date}),
                  "score": calib.score(rows, thr), "thresholds": calib.threshold_table(rows),
                  "skipped": crops.skipped | ({"no_ink": n_no_ink} if n_no_ink else {}),
                  "errors": sum(not r["correct"] for r in rows),
                  "predictions": [{"field_id": s.field_id, "fmt": r.fmt, "text": r.top.text, "answer": r.top.kind,
                                   "confidence": r.top.prob} for s, r in zip(samples, reads, strict=True)]}
    wrong = [(img, cell, rect, r) for (img, cell, rect), r, f in zip(shown, reads, rows, strict=True) if not f["correct"]]
    if errors is not None and wrong:
        out["errors_image"] = str(error_sheet(wrong, Path(errors) / f"{out['model']}-{out['split']}-errors.png"))
    return out


def error_sheet(wrong: list, path: Path, cols: int = 3, tile_w: int = 360, tile_h: int = 96) -> Path:
    """틀린 칸을 한 장에: 크롭과 그 위의 칸(빨강)·글씨 띠(파랑), '정답 → 읽은 글자 (신뢰도)'. 빈 칸은 '_'. 많으면 앞의 MAX_TILES 개."""
    items = wrong[:MAX_TILES]
    label_h = 20
    rows = max(1, math.ceil(len(items) / cols))
    sheet = np.full((rows * (tile_h + label_h), cols * tile_w, 3), 255, np.uint8)
    for i, (img, cell, rect, r) in enumerate(items):
        k_r, k_c = divmod(i, cols)
        vis = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        cv2.rectangle(vis, (int(cell[0]), int(cell[1])), (int(cell[2]), int(cell[3])), (0, 0, 220), 1)
        cv2.rectangle(vis, (rect[0], rect[1]), (rect[2] - 1, rect[3] - 1), (220, 120, 0), 1)
        h, w = vis.shape[:2]
        s = min((tile_w - 8) / w, (tile_h - 4) / h)
        small = cv2.resize(vis, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        y0, x0 = k_r * (tile_h + label_h), k_c * tile_w + 4
        sheet[y0 + 2:y0 + 2 + small.shape[0], x0:x0 + small.shape[1]] = small
        truth = "_" if r.truth == "" else r.truth
        read = "_" if r.top.kind == "empty" else (REJECT if r.top.kind == "reject" else r.top.text)
        cv2.putText(sheet, f"{r.fmt}: {truth} -> {read} ({r.top.prob:.2f})", (x0, y0 + tile_h + 14), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.rectangle(sheet, (k_c * tile_w, y0), ((k_c + 1) * tile_w - 1, y0 + tile_h + label_h - 1), (200, 200, 200), 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    imwrite(path, sheet)
    return path
