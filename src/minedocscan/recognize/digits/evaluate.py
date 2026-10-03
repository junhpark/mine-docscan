"""크롭 단위 평가 — 파이프라인을 돌리지 않고 export-crops 의 폴더에서 바로 (tasks/0003 단계 6). torch 없이 돈다.

  minedocscan recognizer eval --crops DIR --model NAME [--split val|test|train] [--errors [DIR]]

분할: val = train 으로 내보낸 줄 중 모델 카드의 검증 규칙(소금값·비율)이 고르는 날짜 (학습 때와 같은 규칙),
      train = 그 나머지, test = test 로 내보낸 폴더의 줄 (마지막에 한 번 본다).
낸 것: 정확도(전체·값 있는 칸·빈 칸·거절), 값별 표, 많이 틀린 쌍, 신뢰도 구간별 정확도(보정), 임계값별 자동 적재율과
자동 적재 오류율(분자·분모·윌슨 구간), 모델의 기준에서의 수치, illegible 칸 중 자동 적재될 것의 수.
--errors: 틀린 칸을 한 장에 모은 그림 (정답과 읽은 값을 적는다). 현장 글씨가 들어 있으므로 git 작업 트리 안이면 거절한다.

출력은 집계뿐이다 — 셀 하나하나의 값·field_id 는 찍지 않는다 (4.7). 칸마다의 답은 돌려주는 값의 predictions 에만 있고
명령줄은 그것을 출력하지 않는다 (크롭 단위 평가 = 파이프라인 평가인지 시험이 확인하는 데 쓴다).
"""
from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from ...imaging.io import imwrite
from ...review.export import inside_git_tree
from . import calib, data
from .backend import DigitsRecognizer
from .model import REJECT, preprocess, read_answers

SPLITS = ("val", "test", "train")
CONF_BINS = (0.0, 0.5, 0.7, 0.9, 0.95, 0.99, 1.0000001)
MAX_TILES = 240


class EvalError(ValueError):
    pass


def select(crops_dir: str | Path, card: dict, split: str | None) -> tuple[list[data.Sample], data.Crops]:
    """분할의 셀. split=None 이면 폴더의 모든 숫자 칸 (시험용)."""
    if split is not None and split not in SPLITS:
        raise EvalError(f"--split 은 {' | '.join(SPLITS)}: {split}")
    try:
        crops = data.read_crops(crops_dir, allow_test=True, only_split="test" if split == "test" else
                                ("train" if split in ("val", "train") else None))
    except data.CropsError as e:
        raise EvalError(str(e)) from e
    if split == "test" and not crops.samples:
        raise EvalError("이 폴더에는 test 로 내보낸 숫자 칸이 없습니다 — `review export-crops --split test` 로 내보낸 폴더를 주세요")
    if split in ("val", "train"):
        rule = card["data"]["val_rule"]
        tr, va = data.split_val(crops.samples, rule["salt"], float(rule["share"]))
        chosen = va if split == "val" else tr
        if not chosen:
            n_dates = len({s.work_date for s in crops.samples if s.work_date})
            raise EvalError(
                f"이 폴더에는 {split} 날짜의 숫자 칸이 없습니다 (train 으로 내보낸 날짜 {n_dates}일 중 검증 날짜 {len({s.work_date for s in va})}일 "
                f"— 모델 카드의 규칙: 소금값 {rule['salt']!r}, 비율 {rule['share']}). 이 모델은 학습 때 "
                f"{card['validation']['source']} 셀로 검증했다. " + ("--split train 으로 학습 셀의 성적을 보거나, 날짜가 늘면 다시 학습하세요"
                                                                 if split == "val" else "--split val 을 보세요"))
        return chosen, crops
    return crops.samples, crops


def evaluate(crops_dir: str | Path, model_dir: str | Path, split: str | None = "val", threshold: float | None = None,
             errors: str | Path | None = None, trips_max: int | None = None, allow_in_repo: bool = False) -> dict:
    """크롭 폴더를 모델로 읽어 성적을 낸다. errors: 틀린 칸 모아 보기를 쓸 폴더 (None 이면 쓰지 않는다)."""
    if errors is not None and inside_git_tree(errors) and not allow_in_repo:
        raise EvalError(f"{errors} 은 git 작업 트리 안입니다. 틀린 칸 모아 보기에는 현장의 글씨가 들어 있으므로 저장소 밖에 쓰세요")
    rec = DigitsRecognizer(model_dir, threshold=threshold, threshold_source="config")
    samples, crops = select(crops_dir, rec.card, split)
    n_no_ink = sum(not s.inked for s in samples)
    samples = [s for s in samples if s.inked]               # 파이프라인이 인식기에 보내는 칸만 (잉크가 없던 빈 칸은 뺀다)
    if not samples:
        raise EvalError(f"평가할 칸이 없습니다 (잉크가 없던 칸 {n_no_ink}개만 있다)")
    if crops.spec is not None and crops.spec != rec.spec:
        raise EvalError(f"크롭의 규격({crops.spec.describe()})이 모델의 규격({rec.spec.describe()})과 다릅니다 — "
                        "모델과 같은 규격으로 내보낸 폴더를 주세요 (tasks/0003 4.1)")
    preds, imgs = [], []
    for s in samples:
        img = s.image()
        ans = read_answers(rec.net.logits(preprocess(img)), rec.temperature)
        preds.append((ans[0][0], float(ans[0][1]), [a for a, _p in ans]))
        imgs.append(img)
    truths = [s.text for s in samples]
    pc = [(a, c) for a, c, _cand in preds]
    t = rec.threshold
    table = calib.threshold_table(pc, truths, trips_max)
    at = calib.threshold_table(pc, truths, trips_max, grid=(t,))[0] if math.isfinite(t) else None
    out = {
        "model": rec.card["name"], "split": split or "all", "cells": len(samples),
        "dates": len({s.work_date for s in samples if s.work_date}), "spec": rec.spec.describe(),
        "threshold": None if not math.isfinite(t) else t, "threshold_source": rec.threshold_source,
        "accuracy": calib.score(pc, truths),
        "by_value": by_value(pc, truths), "confusions": confusions(pc, truths),
        "calibration": calibration(pc, truths), "thresholds": table, "at_threshold": at,
        "illegible": {"n": sum(y == REJECT for y in truths),
                      "auto": sum(y == REJECT and c >= t and calib.eligible(a, trips_max) for (a, c), y in zip(pc, truths, strict=True))},
        "skipped": crops.skipped | ({"no_ink": n_no_ink} if n_no_ink else {}),
        "errors": sum(a != y for (a, _c), y in zip(pc, truths, strict=True)), "errors_image": None,
        "predictions": [{"field_id": s.field_id, "text": a, "confidence": c} for s, (a, c, _k) in zip(samples, preds, strict=True)],
    }
    wrong = [(im, y, a, c) for im, (a, c), y in zip(imgs, pc, truths, strict=True) if a != y]
    if errors is not None and wrong:                          # 틀린 칸이 없으면 그림을 쓰지 않는다
        out["errors_image"] = str(error_sheet(wrong, Path(errors) / f"{out['model']}-{out['split']}-errors.png"))
    return out


def by_value(pc: list[tuple[str, float]], truths: list[str]) -> list[dict]:
    """정답 값마다 개수와 정확도 (빈 칸은 "", 거절은 "?")."""
    g: dict[str, list[bool]] = {}
    for (a, _c), y in zip(pc, truths, strict=True):
        g.setdefault(y, []).append(a == y)
    key = lambda v: (0, -1) if v == "" else (2, 0) if v == REJECT else (1, int(v))        # noqa: E731
    return [{"value": v, "n": len(ok), "correct": sum(ok), "accuracy": round(sum(ok) / len(ok), 4)}
            for v, ok in sorted(g.items(), key=lambda kv: key(kv[0]))]


def confusions(pc: list[tuple[str, float]], truths: list[str], top: int = 10) -> list[dict]:
    c = Counter((y, a) for (a, _c), y in zip(pc, truths, strict=True) if a != y)
    return [{"truth": y, "read": a, "n": n} for (y, a), n in c.most_common(top)]


def calibration(pc: list[tuple[str, float]], truths: list[str]) -> list[dict]:
    """신뢰도 구간마다: 칸 수, 평균 신뢰도, 정확도. 보정이 맞으면 둘이 가깝다."""
    out = []
    for lo, hi in zip(CONF_BINS, CONF_BINS[1:], strict=False):
        sel = [((a, c), y) for (a, c), y in zip(pc, truths, strict=True) if lo <= c < hi]
        if not sel:
            out.append({"bin": [lo, min(hi, 1.0)], "n": 0, "mean_conf": None, "accuracy": None})
            continue
        out.append({"bin": [lo, min(hi, 1.0)], "n": len(sel), "mean_conf": round(float(np.mean([c for (_a, c), _y in sel])), 4),
                    "accuracy": round(sum(a == y for (a, _c), y in sel) / len(sel), 4)})
    return out


def error_sheet(wrong: list[tuple[np.ndarray, str, str, float]], path: Path, cols: int = 4, tile_w: int = 240,
                tile_h: int = 72) -> Path:
    """틀린 칸을 한 장에: 칸마다 그림과 '정답 → 읽은 값 (신뢰도)'. 빈 칸은 '_', 거절은 '?'. 많으면 앞의 MAX_TILES 개."""
    items = wrong[:MAX_TILES]
    label_h = 20
    rows = max(1, math.ceil(len(items) / cols))
    sheet = np.full((rows * (tile_h + label_h), cols * tile_w), 255, np.uint8)
    for i, (img, y, a, c) in enumerate(items):
        r, k = divmod(i, cols)
        h, w = img.shape[:2]
        s = min((tile_w - 8) / w, (tile_h - 4) / h)
        small = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        y0, x0 = r * (tile_h + label_h), k * tile_w + 4
        sheet[y0 + 2:y0 + 2 + small.shape[0], x0:x0 + small.shape[1]] = small
        text = f"{y or '_'} -> {a or '_'} ({c:.2f})"
        cv2.putText(sheet, text, (x0, y0 + tile_h + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 0, 1, cv2.LINE_AA)
        cv2.rectangle(sheet, (k * tile_w, y0), ((k + 1) * tile_w - 1, y0 + tile_h + label_h - 1), 200, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    imwrite(path, sheet)
    return path
