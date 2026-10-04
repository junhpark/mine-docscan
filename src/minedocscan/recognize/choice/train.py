"""분류기 학습 — torch 는 여기서만 (tasks/0004 단계 4). recognize/meta/train.train_meta 가 부른다.

  minedocscan recognizer train --crops DIR --meta-key operator --name op-v1 --cv 5

1. 종류 = 학습 날짜에 예가 min_examples(기본 3) 이상인 값. 그보다 적은 값은 종류로 두지 않고 "그 밖"(종류 0)으로 학습한다
   — 몇 개였는지(수만) 카드에 적는다. 합성으로 만든 "모르는 사람"의 글씨(tools/synth_meta, 아무 낱말)도 "그 밖"이다.
2. 배치는 종류마다 고르게 뽑는다 (예가 적은 사람이 묻히지 않게). 약한 변형(이동·크기·회전·기울임·대비·흐림·잡음 — 숫자 모델과 같은
   것), 좌우 뒤집기 없음. 정해진 스텝만큼 (예가 적어 검증으로 멈출 때를 고르지 않는다).
3. 기준: --cv 면 묶음마다 나머지로 학습해 그 묶음을 읽고(그 묶음에만 있는 사람은 "모르는 사람"이 된다), 모은 읽기 전체에서 온도·기준.
4. ONNX(배치 1)로 내보내 OpenCV 로 다시 읽어 torch 와 같은지 확인한다. 카드·로그·표준 출력에 이름을 적지 않는다 — 종류 수와
   종류별 개수의 분포만. 이름은 classes.json 에만.

사전 확인 (tasks/0004 4.1, 실제 일보 30쪽): 합성곱 4층 + 전역 최대 풀링, 입력 40×128, 250 스텝, 약한 변형 — 작성자 29/30.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from ...tools import synth_meta
from ..digits import data
from ..digits.model import staging_dir
from ..digits.train import TrainError, _log, augment_batch, code_version, libraries, require_torch
from ..meta import calib
from ..meta.train import META_SPEC, MetaTrainArgs, _geometry, count_summary, plan_reads
from .model import (
    INPUT_H,
    INPUT_W,
    N_INPUT_CHANNELS,
    OTHER,
    choose_from_logits,
    normalize,
    resize_input,
    truth_prob,
)

CHANNELS = (16, 32, 64, 96)
NEGATIVE_SHARE = 0.25          # 합성 "모르는 사람" 의 수 = 실제 셀 수 × 이 몫 (최소 40)


def build_net(torch, n_out: int, channels=CHANNELS):
    """합성곱·배치 정규화·ReLU·최대 풀링 + 완전연결 하나. (1, 1, 40, 128) → (1, n_out)."""
    nn = torch.nn
    c1, c2, c3, c4 = channels

    def cbr(i, o):
        return [nn.Conv2d(i, o, 3, padding=1, bias=False), nn.BatchNorm2d(o), nn.ReLU()]

    return nn.Sequential(*cbr(N_INPUT_CHANNELS, c1), nn.MaxPool2d(2), *cbr(c1, c2), nn.MaxPool2d(2), *cbr(c2, c3),
                         nn.MaxPool2d(2), *cbr(c3, c4), nn.MaxPool2d((INPUT_H // 8, INPUT_W // 8)), nn.Flatten(),
                         nn.Linear(c4, n_out))


def describe_net(channels, n_out: int) -> str:
    c1, c2, c3, c4 = channels
    return (f"conv3x3({c1})-pool2-conv3x3({c2})-pool2-conv3x3({c3})-pool2-conv3x3({c4})-maxpool(전체)-fc({n_out}); "
            "합성곱마다 BN+ReLU; 종류 0 = 그 밖")


def negatives(n: int, seed: int, geom: tuple[int, int], spec) -> list[np.ndarray]:
    """합성 "모르는 사람": 아무 이름 없는 사람이 아무 낱말(4–8자)을 쓴 필드 크롭."""
    rng = np.random.default_rng([seed, 31337])
    letters = sorted(synth_meta.LETTERS)
    fs = synth_meta.FieldSpec(int(geom[0]), int(geom[1]), spec)
    out = []
    for _ in range(n):
        word = "".join(str(rng.choice(letters)) for _ in range(int(rng.integers(4, 9))))
        out.append(resize_input(synth_meta.make_field_crop(rng, word.upper(), "name", fs)))
    return out


def _classes(samples: list[data.Sample], min_examples: int) -> tuple[list[str], int, int]:
    """(종류, 종류가 되지 못한 값의 수, 그 예의 수). 종류의 순서는 정렬 — classes.json 에 그대로."""
    c = Counter(s.text for s in samples if s.verdict == "value")
    keep = sorted(v for v, n in c.items() if n >= min_examples)
    rare = [v for v, n in c.items() if n < min_examples]
    return keep, len(rare), sum(c[v] for v in rare)


def _fit(torch, args: MetaTrainArgs, xs: np.ndarray, ys: np.ndarray, n_out: int, seed: int, progress=None):
    """종류마다 고르게 뽑은 배치로 args.steps 스텝. 돌려주는 값: (망(eval), 기록, 초)."""
    torch.manual_seed(seed)
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    gen = torch.Generator().manual_seed(seed)
    rng = np.random.default_rng([seed, 77])
    net = build_net(torch, n_out)
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=args.steps, pct_start=0.15)
    by_class = [np.where(ys == k)[0] for k in range(n_out)]
    by_class = [ix for ix in by_class if len(ix)]
    lossf = torch.nn.CrossEntropyLoss()
    t0 = time.time()
    log, run = [], 0.0
    for step in range(1, args.steps + 1):
        net.train()
        cls = rng.integers(0, len(by_class), args.batch)
        idx = np.array([by_class[c][rng.integers(0, len(by_class[c]))] for c in cls])
        x = augment_batch(torch, torch.from_numpy(xs[idx]), gen)
        loss = lossf(net(x), torch.from_numpy(ys[idx]).long())
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
        run = 0.9 * run + 0.1 * loss.item() if step > 1 else loss.item()
        if step % args.eval_every == 0 or step == args.steps:
            rec = {"step": step, "loss": round(run, 5), "lr": round(sched.get_last_lr()[0], 6), "seconds": round(time.time() - t0, 1)}
            log.append(rec)
            if progress:
                progress(f"[{step}/{args.steps}] 손실 {run:.4f} · {time.time() - t0:.0f}s")
    net.eval()
    return net, log, time.time() - t0


def export_onnx(torch, net, path: Path) -> str:
    """배치 1. digits/train.export_onnx 와 같은 방식(예전 내보내기 opset 13, 없으면 새 내보내기 opset 18)."""
    import inspect
    import io
    import warnings
    from contextlib import redirect_stdout

    params = inspect.signature(torch.onnx.export).parameters
    x = torch.zeros(1, N_INPUT_CHANNELS, INPUT_H, INPUT_W)
    common = {"opset_version": 13, "input_names": ["x"], "output_names": ["logits"]}
    try:
        with warnings.catch_warnings(), redirect_stdout(io.StringIO()):
            warnings.simplefilter("ignore")
            torch.onnx.export(net, (x,), str(path), **common, **({"dynamo": False} if "dynamo" in params else {}))
        return "torchscript, opset 13"
    except (TypeError, RuntimeError, AttributeError, ImportError):
        if "dynamo" not in params:
            raise
        extra = {"external_data": False} if "external_data" in params else {}
        with redirect_stdout(io.StringIO()):
            torch.onnx.export(net, (x,), str(path), **(common | {"opset_version": 18}), dynamo=True, **extra)
        return "dynamo, opset 18"


def _cv_logits(path: Path, xs: np.ndarray) -> np.ndarray:
    net = cv2.dnn.readNetFromONNX(str(path))
    out = []
    for x in xs:
        net.setInput(np.ascontiguousarray(x[None], dtype=np.float32))
        out.append(np.asarray(net.forward()).reshape(-1))
    return np.stack(out) if out else np.zeros((0, 1), np.float32)


def train_choice(crops: data.Crops, out_dir: Path, args: MetaTrainArgs, *, split_salt: str = "synthetic",
                 progress=_log) -> dict:
    t_start = time.time()
    key = args.keys[0]
    samples = [s for s in crops.samples if s.verdict == "value"]       # 분류기는 읽을 수 없음(illegible)을 쓰지 않는다
    spec = crops.spec or META_SPEC
    geom = _geometry(samples, key)
    plans, method = plan_reads(samples, args, split_salt)
    classes, n_rare, n_rare_ex = _classes(samples if args.cv or not plans else plans[0][0], args.min_examples)
    if not classes:
        raise TrainError(f"종류가 하나도 없습니다 — 학습 날짜에 예가 {args.min_examples}개 이상인 값이 없다 (--min-examples)")
    progress(f"메타 필드 {key} (분류기): 쪽 {len(samples)} (날짜 {len({s.work_date for s in samples})}) · 종류 {len(classes)} "
             f"(예가 {args.min_examples}개 미만이라 '그 밖'으로 둔 값 {n_rare}) · 기준을 정하는 읽기: {method['method']}")
    cache: dict[str, np.ndarray] = {}

    def x_of(s: data.Sample) -> np.ndarray:
        k = str(s.path)
        if k not in cache:
            cache[k] = normalize(resize_input(s.image()))
        return cache[k]

    n_neg = max(40, int(len(samples) * NEGATIVE_SHARE))
    neg = np.stack([normalize(x) for x in negatives(n_neg, args.seed, geom, spec)])
    torch = require_torch()

    def dataset(tr: list[data.Sample], cls: list[str]) -> tuple[np.ndarray, np.ndarray]:
        idx = {c: i + 1 for i, c in enumerate(cls)}
        xs = np.concatenate([np.stack([x_of(s) for s in tr]), neg]) if tr else neg
        ys = np.array([idx.get(s.text, OTHER) for s in tr] + [OTHER] * len(neg), np.int64)
        return xs, ys

    tmp = staging_dir(out_dir)                              # 끊긴 학습의 주인 없는 임시 폴더도 여기서 치운다
    try:
        reads: list[calib.Read] = []

        def read_with(path: Path, cls: list[str], va: list[data.Sample], f) -> list[calib.Read]:
            lg = _cv_logits(path, np.stack([x_of(s) for s in va]))
            return [calib.Read(z, key, cls, s.text, s.work_date, f, {"field_id": s.field_id}) for z, s in zip(lg, va, strict=True)]

        if args.cv:
            for f, (tr, va) in enumerate(plans):
                cls_f, _r, _e = _classes(tr, args.min_examples)
                if not cls_f:
                    continue
                progress(f"읽기 {f + 1}/{len(plans)}: 학습 {len(tr)}쪽(종류 {len(cls_f)}) → {len(va)}쪽을 읽는다")
                net, _log_f, _s = _fit(torch, args, *dataset(tr, cls_f), len(cls_f) + 1, args.seed * 100 + f)
                export_onnx(torch, net, tmp / f"read{f}.onnx")
                reads += read_with(tmp / f"read{f}.onnx", cls_f, va, f)
        final_tr = samples if args.cv or not plans else plans[0][0]
        xs, ys = dataset(final_tr, classes)
        net, log_lines, train_seconds = _fit(torch, args, xs, ys, len(classes) + 1, args.seed, progress)
        exporter = export_onnx(torch, net, tmp / "model.onnx")
        if not args.cv and plans:
            reads = read_with(tmp / "model.onnx", classes, plans[0][1], None)
        probe = xs[: min(len(xs), 200)]
        cv_lg = _cv_logits(tmp / "model.onnx", probe)
        with torch.no_grad():
            th_lg = net(torch.from_numpy(probe)).numpy()
        diff = float(np.abs(cv_lg - th_lg).max()) if len(probe) else 0.0
        same = bool((cv_lg.argmax(1) == th_lg.argmax(1)).all()) if len(probe) else True
        temp, tinfo = calib.fit_temperature(reads, lambda r, t: truth_prob(r.scores, r.candidates, r.truth, t))
        choices = [choose_from_logits(r.scores, r.candidates, temp) for r in reads]
        preds = [(c.value, c.confidence, c.answer) for c in choices]
        truths = [r.truth for r in reads]
        table = calib.threshold_table(preds, truths)
        chosen = calib.choose_threshold(table, args.target_auto_error, args.min_val_auto)
        reason = None if chosen else (calib.why_no_threshold(table, args.target_auto_error, args.min_val_auto) if reads
                                      else "기준을 정할 읽기가 없다 (검증 날짜·묶음의 쪽이 없다)")
        per = Counter(s.text for s in final_tr if s.text in classes)
        n_per = sorted(per.values())
        from ..digits.model import sha256_file

        card = {
            "name": args.name, "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "code_version": code_version(), "spec": spec.to_dict(),
            "input": {"width": INPUT_W, "height": INPUT_H, "channels": N_INPUT_CHANNELS,
                      "preprocess": "recognize/choice/model.py: resize_input → normalize (잉크 채널)"},
            "architecture": {"layers": describe_net(CHANNELS, len(classes) + 1), "channels": list(CHANNELS),
                             "params": int(sum(p.numel() for p in net.parameters())),
                             "onnx_bytes": (tmp / "model.onnx").stat().st_size, "exporter": exporter, "batch": 1},
            "train_args": {k: v for k, v in asdict(args).items() if k in ("steps", "batch", "lr", "val_share", "cv",
                                                                         "min_examples", "target_auto_error", "min_val_auto")}
            | {"negatives": n_neg, "geometry": list(geom), "sampling": "종류마다 고르게 (그 밖 포함)",
               "augment": "이동·크기·회전·기울임·대비·밝기·흐림·잡음 (숫자 모델과 같은 것); 뒤집기 없음"},
            "seed": args.seed,
            "data": {"crops": {"lines": crops.lines, "skipped": crops.skipped, "splits": sorted(crops.splits)},
                     "train": count_summary(final_tr), "all": count_summary(samples),
                     "synthetic": {"cells": n_neg, "kind": "모르는 사람 (그 밖)"},
                     "val_rule": {"salt": data.val_salt(split_salt), "share": args.val_share}},
            "validation": {"source": method["method"], "reads": len(reads),
                           "score": calib.score(preds, truths, [r.candidates for r in reads]),
                           "train_seconds": round(train_seconds, 1), "total_seconds": None,
                           "export_check": {"max_abs_diff": diff, "same_answers": same, "cells": len(probe)},
                           "calibration": tinfo},
            "temperature": temp,
            "auto_accept": {"target": args.target_auto_error, "threshold": None if chosen is None else chosen["threshold"],
                            "met": chosen is not None, "reason": reason, "min_auto": args.min_val_auto,
                            "auto": None if chosen is None else chosen["auto"],
                            "errors": None if chosen is None else chosen["errors"],
                            "upper95": None if chosen is None else chosen["error_ci95"][1],
                            "basis": f"읽기 {len(reads)}번 ({method['method']})",
                            "rule": "오류율이 목표 이하인 가장 낮은 임계값, 자동 적재된 읽기가 min_auto 이상 (ADR 0012, tasks/0004 4.5)",
                            "table": table},
            "libraries": libraries(torch), "model_sha256": sha256_file(tmp / "model.onnx"),
            "meta": {"reader": "choice", "keys": [key],
                     "classes": {"n": len(classes), "per_class": {"min": n_per[0], "median": float(np.median(n_per)),
                                                                  "max": n_per[-1]} if n_per else None,
                                 "dropped_values": n_rare, "dropped_examples": n_rare_ex,
                                 "min_examples": args.min_examples},
                     "cv": method, "candidates": "classes.json 의 종류 (학습 때 본 값)"},
        }
        card["validation"]["total_seconds"] = round(time.time() - t_start, 1)
        (tmp / "card.json").write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
        (tmp / "classes.json").write_text(json.dumps({"reader": "choice", "keys": [key], "classes": classes},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
        with open(tmp / "train-log.jsonl", "w", encoding="utf-8") as fh:
            for rec in log_lines:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if args.cv:
            calib.write_cv_reads(tmp / calib.CV_READS, reads, preds)
        for p in tmp.glob("read*.onnx"):
            p.unlink()
        if out_dir.exists():
            out_dir.rmdir()
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(out_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    aa = card["auto_accept"]
    progress(f"모델: {out_dir} · 종류 {len(classes)} · 읽기 {len(reads)}번 ({method['method']}) · 온도 {temp:g} · 자동 적재 기준 "
             + (f"{aa['threshold']} (자동 적재 {aa['auto']} 중 오류 {aa['errors']}, 95 % 상한 {aa['upper95']:.1%})"
                if aa["met"] else f"없음 ({aa['reason']})") + f" · {time.time() - t_start:.0f}s")
    return card
