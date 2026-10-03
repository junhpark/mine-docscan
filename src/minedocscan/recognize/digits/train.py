"""숫자 인식기 학습 — torch 를 import 하는 곳은 여기뿐이다 (tasks/0003 4.2, 단계 4).

  minedocscan recognizer train --crops DIR --name NAME [--synthetic N] [--seed S] [--val-share 0.2] [--target-auto-error 0.01]

1. 데이터: DIR 의 labels.jsonl + PNG (review export-crops). test 줄이 있거나 규격이 섞였으면 거절 (data.read_crops).
2. 검증 날짜를 뗀다 (train 날짜 안에서 날짜 단위, 소금값 = 사이트 팩의 split_salt + ":val").
3. 학습: 실제 셀 + 합성 셀(tools/synth_cells, 실제 크롭의 규격·칸 크기로)을 배치마다 반반. 실제 셀이 없으면 합성만.
   변형은 약하게(이동·크기·기울기·밝기·흐림), 좌우 뒤집기 없음. 검증 성적이 가장 좋은 스텝의 가중치를 남긴다.
4. ONNX 로 내보내고 **그 파일을 OpenCV 로 다시 읽어** 검증 셀에서 온도·자동 적재 기준·성적을 정한다 (calib.py).
5. <site>/models/NAME/ 에 model.onnx, card.json, train-log.jsonl. 같은 이름이 있으면 멈춘다.

진행은 표준 오류로, 스텝별 수치는 train-log.jsonl 로. 셀 값·field_id·검수자는 어디에도 찍지 않는다 (4.7).
"""
from __future__ import annotations

import io
import json
import math
import multiprocessing
import os
import platform
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from ...imaging.cropspec import CropSpec
from ...review.export import inside_git_tree
from ...tools.synth_cells import CellParams, make_cell, plan_cells
from . import calib, data
from .model import (
    CHARS,
    INPUT_H,
    INPUT_W,
    KINDS,
    N_CLASSES,
    N_INPUT_CHANNELS,
    OnnxNet,
    encode,
    log_softmax,
    normalize,
    normalize_answer,
    read_answers,
    resize_input,
    sha256_file,
)

INSTALL_HINT = ('학습에는 torch 와 onnx 가 필요합니다: pip install -e ".[train]" '
                '(현장 PC 의 추론·파이프라인에는 필요 없습니다)')
DEFAULT_SPEC = CropSpec("source", 1.5, None)          # 4.1 숫자 모델의 기본 규격 (= export-crops 의 기본값)
DEFAULT_GEOMETRY = ((92, 21),)                         # 합성만으로 학습할 때 칸 크기: 실제 운반 칸(괘선 사이 약 100×29)
SYNTH_VAL = 1000                                      # 실제 검증 셀이 없을 때 따로 만드는 합성 검증 셀 수


class TrainError(RuntimeError):
    """학습을 시작할 수 없다 (torch 없음, 데이터 거절, 대상 폴더 …). 메시지만 보여 주고 끝낸다."""


def check_torch() -> None:
    """torch·onnx 가 설치되어 있는지만 본다 (import 하지 않는다 — 합성 셀을 만드는 프로세스 풀이 먼저 fork 해야 한다)."""
    import importlib.util

    missing = [m for m in ("torch", "onnx") if importlib.util.find_spec(m) is None]
    if missing:
        raise TrainError(f"{INSTALL_HINT} — 없는 모듈: {', '.join(missing)}")


def require_torch():
    check_torch()
    try:
        import onnx  # noqa: F401
        import torch
    except ImportError as e:
        raise TrainError(f"{INSTALL_HINT} — 불러올 수 없는 모듈: {e.name} ({e})") from e
    return torch


@dataclass
class TrainArgs:
    name: str
    steps: int = 2500
    batch: int = 64
    lr: float = 3e-3
    synthetic: int | None = None            # 합성 셀 수. None 이면 실제 셀이 있으면 4000, 없으면 8000
    seed: int = 0
    val_share: float = 0.2
    target_auto_error: float = 0.01
    geometry: list[tuple[int, int]] | None = None     # 합성 칸 크기 (실제 셀이 없을 때). None 이면 DEFAULT_GEOMETRY
    channels: tuple[int, int, int, int, int] = (16, 32, 64, 64, 96)
    eval_every: int = 500
    workers: int | None = None              # 합성 셀을 만드는 프로세스 수. None 이면 CPU 수

    def synthetic_n(self, n_real: int) -> int:
        return int(self.synthetic) if self.synthetic is not None else (4000 if n_real else 8000)


# ── 망 ────────────────────────────────────────────────────────────────────
def build_net(torch, channels=(16, 32, 64, 64, 96)):
    """합성곱·배치 정규화·ReLU·최대 풀링만. (1, 3, 48, 128) → (1, 12, 1, 32).
    세로는 최대 풀링으로 접는다 — 같은 자리를 학습되는 합성곱으로 접은 구조는 값 있는 칸 정확도가 0.2 에서 움직이지 않았다 (4.2)."""
    nn = torch.nn
    c1, c2, c3, c4, c5 = channels

    def cbr(i, o, k=3, p=1):
        return [nn.Conv2d(i, o, k, padding=p, bias=False), nn.BatchNorm2d(o), nn.ReLU()]

    return nn.Sequential(
        *cbr(N_INPUT_CHANNELS, c1), nn.MaxPool2d(2),
        *cbr(c1, c2), nn.MaxPool2d(2),
        *cbr(c2, c3), *cbr(c3, c4), nn.MaxPool2d((2, 1)),
        *cbr(c4, c5), nn.MaxPool2d((INPUT_H // 8, 1)),
        *cbr(c5, c5, (1, 3), (0, 1)),
        nn.Conv2d(c5, N_CLASSES, 1),
    )


def describe_net(channels) -> str:
    c1, c2, c3, c4, c5 = channels
    return (f"conv3x3({c1})-pool2-conv3x3({c2})-pool2-conv3x3({c3})-conv3x3({c4})-pool(2,1)-conv3x3({c5})"
            f"-maxpool({INPUT_H // 8},1)-conv1x3({c5})-conv1x1({N_CLASSES}); 합성곱마다 BN+ReLU; CTC, T={INPUT_W // 4}")


# ── 합성 셀 ────────────────────────────────────────────────────────────────
def _gen_chunk(job) -> list[tuple[np.ndarray, str]]:
    seed, params, items = job
    cv2.setNumThreads(1)
    out = []
    for i, kind, nb in items:
        c = make_cell(np.random.default_rng([seed, i]), params, kind, nb)
        out.append((resize_input(c.image), c.text))
    return out


def synth_pool(n: int, seed: int, geometry, spec: CropSpec, workers: int | None = None,
               chunk: int = 250) -> tuple[list[np.ndarray], list[str]]:
    """합성 셀 n 개를 칸 크기마다 고르게 나눠 만든다 (모델 입력 크기로 줄인 회색조). 같은 인자면 같은 셀."""
    geometry = list(geometry)
    jobs = []
    for g, (w, h) in enumerate(geometry):
        n_g = n // len(geometry) + (1 if g < n % len(geometry) else 0)
        if n_g <= 0:
            continue
        params = CellParams(cell_w=int(w), cell_h=int(h), spec=spec)
        seed_g = seed * 100 + g
        plan = [(i, k, nb) for i, (k, nb) in enumerate(plan_cells(n_g, seed_g, params))]
        jobs += [(seed_g, params, plan[j:j + chunk]) for j in range(0, len(plan), chunk)]
    workers = max(1, min(workers or os.cpu_count() or 1, len(jobs)))
    if workers == 1:
        parts = [_gen_chunk(j) for j in jobs]
    else:
        # torch 를 불러오기 전이면 fork (빠르고, 부른 쪽 스크립트를 다시 실행하지 않는다). torch 를 쓴 뒤에 fork 하면
        # 자식이 torch 의 스레드 잠금에서 멈춘다 — 그때는 spawn. train() 은 torch 를 합성 셀을 다 만든 뒤에 불러온다
        fork_ok = "torch" not in sys.modules and sys.platform != "darwin"
        method = "fork" if fork_ok and "fork" in multiprocessing.get_all_start_methods() else "spawn"
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context(method)) as ex:
            parts = list(ex.map(_gen_chunk, jobs))
    flat = [x for p in parts for x in p]
    return [x[0] for x in flat], [x[1] for x in flat]


# ── 변형 ──────────────────────────────────────────────────────────────────
def augment_batch(torch, ink, gen):
    """약한 변형을 배치째로 (정규화한 잉크 채널 (B, 1, H, W) 에서): 이동·크기·회전·기울임·대비·밝기·흐림·잡음.
    좌우 뒤집기는 하지 않는다 (숫자의 모양이 바뀐다). 추론 때의 입력(model.normalize)은 이 변형이 없는 것이다."""
    F = torch.nn.functional
    n = ink.shape[0]

    def u(lo: float, hi: float):
        return lo + (hi - lo) * torch.rand(n, generator=gen)

    ang, sc, sh = u(-3, 3) * (math.pi / 180), u(0.9, 1.1), u(-0.12, 0.12)
    cos, sin = torch.cos(ang) / sc, torch.sin(ang) / sc
    theta = torch.stack([torch.stack([cos, -sin + sh, u(-0.08, 0.08)], 1),         # 이동: 가로 ±4 %, 세로 ±6 %
                         torch.stack([sin, cos, u(-0.12, 0.12)], 1)], 1)
    grid = F.affine_grid(theta, list(ink.shape), align_corners=False)
    x = F.grid_sample(ink, grid, mode="bilinear", padding_mode="border", align_corners=False)
    x = x * u(0.7, 1.2).view(n, 1, 1, 1) + u(-0.1, 0.1).view(n, 1, 1, 1)
    k = torch.tensor([0.25, 0.5, 0.25])
    blurred = F.conv2d(F.pad(x, (1, 1, 1, 1), mode="replicate"), (k[:, None] * k[None, :]).view(1, 1, 3, 3))
    x = torch.where((torch.rand(n, generator=gen) < 0.3).view(n, 1, 1, 1), blurred, x)
    noisy = (torch.rand(n, generator=gen) < 0.3).view(n, 1, 1, 1)
    x = x + noisy * torch.randn(x.shape, generator=gen) * u(0.02, 0.08).view(n, 1, 1, 1)
    return x.clamp(-0.3, 1.2)


def _ink(small: list[np.ndarray]) -> np.ndarray:
    """모델 입력 크기의 회색조들 → 정규화한 잉크 채널 (N, H, W)."""
    return np.stack([normalize(x)[0] for x in small]) if small else np.zeros((0, INPUT_H, INPUT_W), np.float32)


# ── 학습 ──────────────────────────────────────────────────────────────────
def _targets(torch, texts: list[str]):
    ys = [encode(t) for t in texts]
    flat = [c for y in ys for c in y]
    return torch.tensor(flat, dtype=torch.long), torch.tensor([len(y) for y in ys], dtype=torch.long)


def _greedy(logits: np.ndarray) -> str:
    """(C, T) → 가장 높은 경로를 접은 답 (학습 중 검증용. 운용은 빔 탐색 — model.read_answers)."""
    best = logits.argmax(axis=0)
    out, prev = [], 0
    for c in best:
        if c != prev and c != 0:
            out.append(CHARS[c - 1])
        prev = c
    return normalize_answer("".join(out))


def _torch_logits(torch, net, xs: np.ndarray, batch: int = 256) -> np.ndarray:
    net.eval()
    outs = []
    with torch.no_grad():
        for i in range(0, len(xs), batch):
            outs.append(net(torch.from_numpy(xs[i:i + batch])).squeeze(2).numpy())
    return np.concatenate(outs) if outs else np.zeros((0, N_CLASSES, INPUT_W // 4), np.float32)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def train(crops_dir: str | Path | None, out_dir: str | Path, args: TrainArgs, *, split_salt: str = "synthetic",
          trips_max: int | None = None, allow_in_repo: bool = False, progress=_log) -> dict:
    """학습하고 out_dir(모델 폴더)에 model.onnx, card.json, train-log.jsonl 을 쓴다. 돌려주는 값: 카드."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise TrainError(f"같은 이름의 모델이 있습니다: {out_dir} — 덮어쓰지 않습니다. 다른 --name 을 주세요")
    if inside_git_tree(out_dir.parent if not out_dir.exists() else out_dir) and not allow_in_repo:
        raise TrainError(f"{out_dir} 은 git 작업 트리 안입니다. 현장 글씨로 학습한 모델은 사이트 팩에 둡니다 "
                         "(합성 셀만으로 만든 시험용 모델이면 --allow-in-repo)")
    if not 0 <= args.val_share < 1:
        raise TrainError(f"--val-share 는 0 이상 1 미만: {args.val_share}")
    if not 0 < args.target_auto_error < 1:
        raise TrainError(f"--target-auto-error 는 0 초과 1 미만: {args.target_auto_error}")
    t_start = time.time()
    rng = np.random.default_rng([args.seed, 99])

    # 1–2. 실제 셀과 검증 날짜
    crops = None
    real_tr: list[data.Sample] = []
    real_va: list[data.Sample] = []
    salt = data.val_salt(split_salt)
    if crops_dir is not None:
        try:
            crops = data.read_crops(crops_dir)
        except data.CropsError as e:
            raise TrainError(str(e)) from e
        real_tr, real_va = data.split_val(crops.samples, salt, args.val_share)
        tr_dates = {s.work_date for s in real_tr if s.work_date}
        va_dates = {s.work_date for s in real_va}
        assert not tr_dates & va_dates, "검증 날짜와 학습 날짜가 겹친다"
    check_torch()                    # 데이터를 거절할 일이 있으면 그것부터 말한다 (torch 가 없는 컴퓨터에서도)
    spec = crops.spec if crops and crops.spec else DEFAULT_SPEC
    if crops is not None and crops.spec is None and crops.samples:
        raise TrainError("크롭 규격을 알 수 없습니다")
    if real_tr:
        geometry = _geometry_of(real_tr + real_va)
    else:
        geometry = [tuple(g) for g in (args.geometry or DEFAULT_GEOMETRY)]
    progress(f"실제 셀: 학습 {len(real_tr)} (날짜 {len({s.work_date for s in real_tr})}), 검증 {len(real_va)} "
             f"(날짜 {len({s.work_date for s in real_va})}) · 규격 {spec.describe()}")

    # 3. 합성 셀
    n_syn = args.synthetic_n(len(real_tr))
    t0 = time.time()
    syn_x, syn_y = synth_pool(n_syn, args.seed, geometry, spec, args.workers) if n_syn else ([], [])
    n_syn = len(syn_x)
    progress(f"합성 셀 {n_syn}개 ({', '.join(f'{w}x{h}' for w, h in geometry)}) — {time.time() - t0:.1f}s")
    if not real_tr and not n_syn:
        raise TrainError("학습할 셀이 없습니다 (실제 셀 0, --synthetic 0)")
    tr_ink = _ink([resize_input(s.image()) for s in real_tr])
    tr_y = [s.text for s in real_tr]
    syn_ink = _ink(syn_x)
    del syn_x
    if real_va:
        va_small = [resize_input(s.image()) for s in real_va]
        va_y = [s.text for s in real_va]
        val_source = "real"
    else:
        va_small, va_y = synth_pool(SYNTH_VAL, args.seed + 7_777, geometry, spec, args.workers)
        val_source = "synthetic"
    va_x = np.stack([normalize(x) for x in va_small]) if va_small else np.zeros((0, 3, INPUT_H, INPUT_W), np.float32)

    # 학습 (torch 는 합성 셀을 다 만든 뒤에 불러온다 — 위의 프로세스 풀이 fork 한다)
    torch = require_torch()
    torch.manual_seed(args.seed)
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    gen = torch.Generator().manual_seed(args.seed)
    coords = torch.from_numpy(normalize(np.zeros((INPUT_H, INPUT_W), np.uint8))[1:])[None]     # 위치 채널 (1, 2, H, W)
    net = build_net(torch, args.channels).to(memory_format=torch.channels_last)     # CPU 합성곱이 1.3–1.5배 빠르다
    n_params = int(sum(p.numel() for p in net.parameters()))
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=args.steps, pct_start=0.15)
    ctc = torch.nn.CTCLoss(blank=0, reduction="sum", zero_infinity=True)
    log_lines: list[dict] = []
    best = {"acc": -1.0, "loss": float("inf"), "step": 0, "state": None}
    t_train = time.time()
    run_loss = 0.0
    for step in range(1, args.steps + 1):
        net.train()
        if len(tr_ink) and len(syn_ink):
            n_r = args.batch // 2
            idx_r = rng.integers(0, len(tr_ink), n_r)
            idx_s = rng.integers(0, len(syn_ink), args.batch - n_r)
            ink = np.concatenate([tr_ink[idx_r], syn_ink[idx_s]])
            texts = [tr_y[i] for i in idx_r] + [syn_y[i] for i in idx_s]
        else:
            pool_x, pool_y = (tr_ink, tr_y) if len(tr_ink) else (syn_ink, syn_y)
            idx = rng.integers(0, len(pool_x), args.batch)
            ink, texts = pool_x[idx], [pool_y[i] for i in idx]
        x = augment_batch(torch, torch.from_numpy(ink)[:, None], gen)
        xb = torch.cat([x, coords.expand(len(texts), -1, -1, -1)], 1).contiguous(memory_format=torch.channels_last)
        tg, tl = _targets(torch, texts)
        logp = net(xb).squeeze(2).permute(2, 0, 1).log_softmax(2)                     # (T, N, C)
        il = torch.full((len(texts),), logp.shape[0], dtype=torch.long)
        loss = ctc(logp, tg, il, tl) / len(texts)
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(net.parameters(), 5.0)
        opt.step()
        sched.step()
        run_loss = 0.9 * run_loss + 0.1 * loss.item() if step > 1 else loss.item()
        if step % args.eval_every == 0 or step == args.steps:
            lg = _torch_logits(torch, net, va_x)
            acc = float(np.mean([_greedy(z) == y for z, y in zip(lg, va_y, strict=True)])) if len(va_y) else 0.0
            vloss = float(np.mean([min(calib.ctc_nll(log_softmax(z), encode(y)), 50.0) for z, y in zip(lg, va_y, strict=True)])) \
                if len(va_y) else 0.0
            rec = {"step": step, "loss": round(run_loss, 5), "lr": round(sched.get_last_lr()[0], 6),
                   "val_acc": round(acc, 5), "val_loss": round(vloss, 5), "seconds": round(time.time() - t_train, 1)}
            log_lines.append(rec)
            progress(f"[{step}/{args.steps}] 손실 {run_loss:.4f} · 검증 정확도 {acc:.4f} · 검증 손실 {vloss:.4f} · "
                     f"{time.time() - t_train:.0f}s")
            if (acc, -vloss) > (best["acc"], -best["loss"]):
                best = {"acc": acc, "loss": vloss, "step": step,
                        "state": {k: v.detach().clone() for k, v in net.state_dict().items()}}
    train_seconds = time.time() - t_train
    if best["state"] is not None:
        net.load_state_dict(best["state"])
    net.eval()

    # 4. ONNX 로 내보내고 OpenCV 로 다시 읽어 검증
    tmp = out_dir.parent / f".{out_dir.name}.tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        exporter = export_onnx(torch, net, tmp / "model.onnx")
        onnx_net = OnnxNet(tmp / "model.onnx")
        cv_logits = onnx_net.logits_many(va_x)
        th_logits = _torch_logits(torch, net, va_x)
        diff = float(np.abs(cv_logits - th_logits).max()) if len(va_x) else 0.0
        same = all(read_answers(a, 1.0)[0][0] == read_answers(b, 1.0)[0][0] for a, b in zip(cv_logits, th_logits, strict=True))
        temp, tinfo = calib.fit_temperature(list(cv_logits), va_y)
        preds = calib.predict(list(cv_logits), temp)
        pc = [(a, c) for a, c, _cand in preds]
        table = calib.threshold_table(pc, va_y, trips_max)
        chosen = calib.choose_threshold(table, args.target_auto_error)
        val_score = calib.score(pc, va_y)

        card = {
            "name": args.name,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "code_version": code_version(),
            "spec": spec.to_dict(),
            "input": {"width": INPUT_W, "height": INPUT_H, "channels": N_INPUT_CHANNELS,
                      "preprocess": "recognize/digits/model.py: resize_input → normalize (잉크, 세로 위치, 가로 위치)"},
            "chars": CHARS,
            "kinds": list(KINDS),
            "architecture": {"layers": describe_net(args.channels), "channels": list(args.channels), "params": n_params,
                             "onnx_bytes": (tmp / "model.onnx").stat().st_size, "exporter": exporter, "batch": 1},
            "train_args": {k: v for k, v in asdict(args).items() if k not in ("name", "seed", "workers")}
            | {"synthetic": n_syn, "geometry": [list(g) for g in geometry],
               "augment": "이동 ±4%/±6%, 크기 0.9–1.1, 회전 ±3°, 기울임 ±0.12, 대비 0.7–1.2, 밝기 ±0.1, 흐림·잡음 30%; 뒤집기 없음",
               "mix": "실제:합성 = 1:1 (배치 기준)" if len(tr_ink) and n_syn else ("합성만" if n_syn else "실제만")},
            "seed": args.seed,
            "data": {
                "crops": None if crops is None else {"lines": crops.lines, "skipped": crops.skipped,
                                                     "splits": sorted(crops.splits)},
                "train": data.summarize(real_tr), "val": data.summarize(real_va),
                "synthetic": {"cells": n_syn, "geometry": [list(g) for g in geometry],
                              "val_cells": 0 if real_va else len(va_y)},
                "val_rule": {"salt": salt, "share": args.val_share,
                             "rule": "sha256(salt:날짜) 앞 32비트 / 2^32 < share 이면 검증 (evaluate/split.py)"},
            },
            "validation": {"source": val_source, "cells": len(va_y), "best_step": best["step"],
                           "score": val_score, "train_seconds": round(train_seconds, 1),
                           "total_seconds": None,
                           "export_check": {"max_abs_diff": diff, "same_answers": bool(same), "cells": len(va_y)},
                           "calibration": tinfo},
            "temperature": temp,
            "auto_accept": {"target": args.target_auto_error, "threshold": None if chosen is None else chosen["threshold"],
                            "met": chosen is not None, "trips_max": trips_max, "basis": f"검증 셀 ({val_source})",
                            "rule": "오류율(자동 적재된 칸 중 정답과 다른 비율)이 목표 이하인 가장 낮은 임계값 (tasks/0003 4.6)",
                            "table": table},
            "libraries": libraries(torch),
            "model_sha256": sha256_file(tmp / "model.onnx"),
        }
        card["validation"]["total_seconds"] = round(time.time() - t_start, 1)
        (tmp / "card.json").write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
        with open(tmp / "train-log.jsonl", "w", encoding="utf-8") as f:
            for rec in log_lines:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if out_dir.exists():
            out_dir.rmdir()                                          # 빈 폴더만 (위에서 확인)
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(out_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    progress(f"모델: {out_dir} · 검증 {val_source} {len(va_y)}셀 · 온도 {temp:g} · 자동 적재 기준 "
             f"{'없음 (목표를 만족하는 임계값 없음)' if chosen is None else chosen['threshold']} · "
             f"{time.time() - t_start:.0f}s")
    return card


def _geometry_of(samples: list[data.Sample], k: int = 6) -> list[tuple[int, int]]:
    """실제 셀의 칸 크기(bbox) 중 많은 것 k 개 — 합성 셀을 그 크기로 만든다."""
    from collections import Counter

    c = Counter((s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1]) for s in samples if s.bbox)
    return [g for g, _n in c.most_common(k)] or list(DEFAULT_GEOMETRY)


def export_onnx(torch, net, path: Path) -> str:
    """배치 1 고정. 가능하면 예전(TorchScript) 내보내기로 opset 13 — 오래된 OpenCV 도 읽는다. 그것이 없어진 torch 에서는
    새 내보내기(opset 18). 어느 쪽이었는지 돌려준다."""
    import warnings

    x = torch.zeros(1, N_INPUT_CHANNELS, INPUT_H, INPUT_W)
    try:
        with warnings.catch_warnings(), redirect_stdout(io.StringIO()):
            warnings.simplefilter("ignore")
            torch.onnx.export(net, (x,), str(path), opset_version=13, input_names=["x"], output_names=["logits"],
                              dynamo=False)
        return "torchscript, opset 13"
    except (TypeError, RuntimeError, AttributeError, ImportError):
        with redirect_stdout(io.StringIO()):
            torch.onnx.export(net, (x,), str(path), opset_version=18, input_names=["x"], output_names=["logits"],
                              dynamo=True, external_data=False)
        return "dynamo, opset 18"


def code_version() -> dict:
    from importlib.metadata import PackageNotFoundError, version

    try:
        v = version("minedocscan")
    except PackageNotFoundError:
        v = "unknown"
    commit = None
    try:
        import subprocess

        here = Path(__file__).resolve().parent
        r = subprocess.run(["git", "-C", str(here), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=5, check=False)
        commit = r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return {"minedocscan": v, "git": commit}


def libraries(torch) -> dict:
    import onnx

    return {"python": platform.python_version(), "torch": torch.__version__, "onnx": onnx.__version__,
            "numpy": np.__version__, "opencv": cv2.__version__}
