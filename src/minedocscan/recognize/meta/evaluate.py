"""메타 필드 모델의 크롭 단위 평가 (recognizer eval) — 파이프라인을 돌리지 않고 `export-crops --meta` 의 폴더에서. torch 없이.

0003 의 출력(정확도, 신뢰도 구간별 정확도, 임계값별 자동 적재율·오류율, 모델의 기준에서의 수치, 틀린 칸 모아 보기)에
목록에 없는 값으로 답한 수와, 정답이 목록 밖이었던 쪽의 처리가 더해진다.
값별 표·많이 틀린 쌍은 내지 않는다 — 값이 이름·차량번호다 (4.7). 틀린 칸 모아 보기에도 값을 적지 않는다 (맞음/틀림 표시만).

분할: val = 모델이 기준을 정한 읽기와 같은 날짜 — --cv 로 만든 모델은 train 날짜 전부로 학습했으므로 다시 잴 수 없다
(카드의 읽기 수치를 본다). train = 나머지, test = test 로 내보낸 폴더 (마지막에 한 번).
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from ...imaging.io import imwrite
from ...review.export import inside_git_tree
from ..digits import data
from ..digits.evaluate import CONF_BINS, MAX_TILES, SPLITS, EvalError
from . import calib
from .model import MetaModel


def evaluate_meta(crops_dir: str | Path, model_dir: str | Path, split: str | None = "val", site=None,
                  errors: str | Path | None = None, allow_in_repo: bool = False) -> dict:
    if errors is not None and inside_git_tree(errors) and not allow_in_repo:
        raise EvalError(f"{errors} 은 git 작업 트리 안입니다. 틀린 칸 모아 보기에는 이름·차량번호 글씨가 들어 있으므로 저장소 밖에 쓰세요")
    if split is not None and split not in SPLITS:
        raise EvalError(f"--split 은 {' | '.join(SPLITS)}: {split}")
    m = MetaModel(model_dir)
    method = m.card["meta"].get("cv") or {}
    if split == "val" and method.get("method", "").startswith("cv"):
        raise EvalError(f"이 모델은 --cv {method.get('k')} 로 기준을 정하고 train 날짜 전부로 학습했습니다 — 검증 수치는 카드의 읽기 "
                        f"{m.card['validation']['reads']}번입니다 (`recognizer list`). 크롭으로는 --split test 를 보세요")
    try:
        crops = data.read_crops(crops_dir, allow_test=True, meta_keys=tuple(m.keys),
                                only_split="test" if split == "test" else ("train" if split in ("val", "train") else None))
    except data.CropsError as e:
        raise EvalError(str(e)) from e
    samples = crops.samples
    if split in ("val", "train"):
        salt, share = method.get("salt"), float(method.get("share", 0.2))
        va = {s.work_date for s in samples if data.is_val_date(s.work_date, salt, share)} if salt else set()
        samples = [s for s in samples if (s.work_date in va) == (split == "val")]
    if not samples:
        raise EvalError(f"이 폴더에는 {split or 'all'} 날짜의 {', '.join(m.keys)} 필드가 없습니다")
    if crops.spec is not None and crops.spec != m.spec:
        raise EvalError(f"크롭의 규격({crops.spec.describe()})이 모델의 규격({m.spec.describe()})과 다릅니다")
    if m.reader == "digits":
        samples = [s for s in samples if s.text.isdigit() or s.text == "?"]
    cands = {k: m.candidates(k, site) for k in m.keys}
    preds, truths, cand_lists, imgs = [], [], [], []
    for s in samples:
        img = s.image()
        c = m.read(img, s.meta_key, cands[s.meta_key])
        preds.append((c.value, c.confidence, c.answer))
        truths.append(s.text)
        cand_lists.append(cands[s.meta_key])
        imgs.append(img)
    t = m.threshold
    table = calib.threshold_table(preds, truths)
    at = calib.threshold_table(preds, truths, grid=(t,))[0] if math.isfinite(t) else None
    out = {"model": m.name, "reader": m.reader, "keys": m.keys, "split": split or "all", "cells": len(samples),
           "dates": len({s.work_date for s in samples if s.work_date}), "spec": m.spec.describe(),
           "threshold": None if not math.isfinite(t) else t, "score": calib.score(preds, truths, cand_lists),
           "by_key": {k: calib.score([p for p, s in zip(preds, samples, strict=True) if s.meta_key == k],
                                     [y for y, s in zip(truths, samples, strict=True) if s.meta_key == k],
                                     [c for c, s in zip(cand_lists, samples, strict=True) if s.meta_key == k]) for k in m.keys},
           "calibration": _calibration(preds, truths), "thresholds": table, "at_threshold": at,
           "candidates": {k: len(v) for k, v in cands.items()}, "skipped": crops.skipped, "errors_image": None,
           "predictions": [{"field_id": s.field_id, "value": v, "confidence": c, "answer": a}
                           for s, (v, c, a) in zip(samples, preds, strict=True)]}
    wrong = [(im, c) for im, (v, c, _a), y in zip(imgs, preds, truths, strict=True) if v != y]
    out["errors"] = len(wrong)
    if errors is not None and wrong:
        out["errors_image"] = str(_sheet(wrong, Path(errors) / f"{m.name}-{out['split']}-errors.png"))
    return out


def _calibration(preds, truths) -> list[dict]:
    out = []
    for lo, hi in zip(CONF_BINS, CONF_BINS[1:], strict=False):
        sel = [(v == y) for (v, c, a), y in zip(preds, truths, strict=True) if a == "value" and lo <= c < hi]
        confs = [c for (_v, c, a) in preds if a == "value" and lo <= c < hi]
        out.append({"bin": [lo, min(hi, 1.0)], "n": len(sel), "mean_conf": round(float(np.mean(confs)), 4) if confs else None,
                    "accuracy": round(sum(sel) / len(sel), 4) if sel else None})
    return out


def _sheet(wrong: list[tuple[np.ndarray, float]], path: Path, cols: int = 3, tile_w: int = 320, tile_h: int = 80) -> Path:
    """틀린 칸을 한 장에. 값은 적지 않는다 — 신뢰도만 (이름·차량번호를 그림 밖으로 꺼내지 않는다)."""
    items = wrong[:MAX_TILES]
    label_h = 18
    rows = max(1, math.ceil(len(items) / cols))
    sheet = np.full((rows * (tile_h + label_h), cols * tile_w), 255, np.uint8)
    for i, (img, c) in enumerate(items):
        r, k = divmod(i, cols)
        h, w = img.shape[:2]
        s = min((tile_w - 8) / w, (tile_h - 4) / h)
        small = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        y0, x0 = r * (tile_h + label_h), k * tile_w + 4
        sheet[y0 + 2:y0 + 2 + small.shape[0], x0:x0 + small.shape[1]] = small
        cv2.putText(sheet, f"{c:.2f}", (x0, y0 + tile_h + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.4, 0, 1, cv2.LINE_AA)
        cv2.rectangle(sheet, (k * tile_w, y0), ((k + 1) * tile_w - 1, y0 + tile_h + label_h - 1), 200, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    imwrite(path, sheet)
    return path
