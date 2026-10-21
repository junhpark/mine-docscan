"""소수·시각 모델 학습 — torch 는 여기서만 import 한다 (tasks/0011 4.3·4.4·4.5·4.6).

  minedocscan recognizer train --readings [--crops DIR] --name NAME [--cv K] [--synthetic N] [--synthetic-geometry …]

숫자 모델(digits/train.py)과 같은 망·변형·내보내기·카드의 규칙. 다른 것:
  · 입력은 글씨 띠(readings/model.band — 48 × 320, 가로 위치 채널은 칸마다), 글자는 blank + 0–9 . : ~ - ?
  · 실제 칸의 정답은 정규화한 값이라 목표가 여럿이다 (formats.spellings — 4.4): 손실 = −log Σ P(표기). (칸, 표기) 쌍을 펼쳐 CTC 를
    reduction='none' 으로 한 번에 계산하고 칸마다 log-sum-exp. 합성 칸은 적힌 글자 하나
  · 합성 칸은 tools/synth_readings (형식마다 실제 칸 크기로, 없으면 --synthetic-geometry)
  · 기준: 검증 날짜(소금값 + ":val") 또는 --cv K (메타 모델과 같이 — 날짜 묶음마다 나머지로 학습해 그 묶음을 읽는다, cv-reads.jsonl).
    자동 적재 대상 = 값(점으로 쓴 시각이 아닌 것)과 빈 칸, 기준은 하나 (형식마다 두지 않는다). 현장 칸으로 학습했는데 검증할 현장 칸이
    없으면 기준을 정하지 않는다. 합성만으로 학습하면 합성 검증 칸으로 정한다 (시험용 모델)
진행은 표준 오류로 — 수와 모양만, 값·field_id·검수자는 어디에도 찍지 않는다 (7절).
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from ...imaging.cropspec import CropSpec
from ...review.export import inside_git_tree
from ...tools.synth_readings import FORMAT_SHARES, ReadingParams, make_reading, plan_readings, plan_set
from ..digits import data as digits_data
from ..digits.calib import MIN_AUTO
from ..digits.model import OnnxNet, clean_orphan_staging, sha256_file, staging_dir
from ..digits.train import (
    TrainError,
    _check_export,
    _log,
    _torch_logits,
    augment_batch,
    build_net,
    check_torch,
    code_version,
    describe_net,
    export_onnx,
    libraries,
    require_torch,
)
from . import calib, data
from .model import (
    BAND,
    BEAM,
    CHARS,
    FORMATS,
    INPUT_H,
    INPUT_W,
    N_INPUT_CHANNELS,
    READER,
    REJECT,
    SHAPE,
    band,
    encode,
    shape_of,
)

DEFAULT_SPEC = CropSpec("source", 1.5, None)          # 숫자 모델의 기본 규격과 같다 — export-crops 한 번으로 두 모델의 크롭 (4.3)
# 합성만으로 학습할 때 칸 크기 (템플릿 px): 합성 가동 일보의 칸(계기 410×38·310×38·362×38, 근무 시각 432×52 — 템플릿 bbox),
# 1절 나의 실제 크기(계기 폭 112–297 × 51, 근무 시각 308 × 62·99), V2 의 유류일지·환경일지 칸(212×82, 192×82)
DEFAULT_GEOMETRY = {"reading": [(410, 38), (310, 38), (362, 38), (112, 51), (200, 51), (297, 51)],
                    "time_range": [(432, 52), (308, 62), (308, 99)],
                    "decimal": [(192, 82), (180, 40)], "time": [(212, 82), (150, 40)]}
SYNTH_VAL = 1000                  # 실제 검증 칸이 없을 때 따로 만드는 합성 검증 칸 수
CV_READS = "cv-reads.jsonl"


@dataclass
class ReadingTrainArgs:
    name: str
    steps: int = 4000                         # 배치 32 에 CPU 4코어에서 걸음당 약 0.18초 — 합성 칸 만들기와 합쳐 15분 안 (4.6)
    batch: int = 32
    lr: float = 4e-3
    synthetic: int | None = None             # 합성 칸 수. None 이면 실제 칸이 있으면 6000, 없으면 12000
    seed: int = 0
    val_share: float = 0.2
    target_auto_error: float = 0.01
    min_val_auto: int = MIN_AUTO              # 기준을 정하는 데 필요한, 그 임계값에서 자동 적재된 검증 칸의 최소 수
    geometry: dict[str, list[tuple[int, int]]] | None = None       # 형식 → 칸 크기들 (실제 칸이 없는 형식)
    channels: tuple[int, int, int, int, int] = (16, 32, 64, 64, 96)
    eval_every: int = 500
    workers: int | None = None
    real_share: float = 0.5
    cv: int | None = None

    def synthetic_n(self, n_real: int) -> int:
        return int(self.synthetic) if self.synthetic is not None else (6000 if n_real else 12000)


# ── 합성 칸 ────────────────────────────────────────────────────────────────
def _gen_chunk(job) -> list[tuple]:
    seed, params, items = job
    cv2.setNumThreads(1)
    out = []
    for i, kind, nb, label, mode in items:
        c = make_reading(np.random.default_rng([seed, i]), params, kind, nb, label, mode)
        b = band(c.image, c.cell, float(params.spec.scale))
        out.append((b.image, b.left, b.right, c.text, c.fmt, c.kind))
    return out


def synth_pool(n: int, seed: int, geometry: dict[str, list[tuple[int, int]]], spec: CropSpec, workers: int | None = None,
               chunk: int = 200) -> dict:
    """합성 칸 n 개를 형식(FORMAT_SHARES)·칸 크기마다 나눠 만들어 글씨 띠로 (같은 인자면 같은 칸).
    돌려주는 값: {"images": (N, 48, 320) uint8, "left", "right": (N,), "texts", "fmts", "kinds"}."""
    jobs = []
    for g, (fmt, (w, h), k) in enumerate(plan_set(n, seed, geometry, FORMAT_SHARES)):
        params = ReadingParams(fmt=fmt, cell_w=w, cell_h=h, spec=spec)
        seed_g = seed * 100 + g
        plan = [(i, *job) for i, job in enumerate(plan_readings(k, seed_g, params))]
        jobs += [(seed_g, params, plan[j:j + chunk]) for j in range(0, len(plan), chunk)]
    workers = max(1, min(workers or os.cpu_count() or 1, len(jobs) or 1))
    if workers == 1:
        parts = [_gen_chunk(j) for j in jobs]
    else:                       # 언제나 spawn (digits/train.synth_pool 과 같은 까닭)
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as ex:
            parts = list(ex.map(_gen_chunk, jobs))
    flat = [x for p in parts for x in p]
    return {"images": np.stack([x[0] for x in flat]) if flat else np.zeros((0, INPUT_H, INPUT_W), np.uint8),
            "left": np.array([x[1] for x in flat], np.float32), "right": np.array([x[2] for x in flat], np.float32),
            "texts": [x[3] for x in flat], "fmts": [x[4] for x in flat], "kinds": [x[5] for x in flat]}


def parse_geometry(text: str | None) -> dict[str, list[tuple[int, int]]] | None:
    """--synthetic-geometry: 항목은 WxH(모든 형식) 또는 형식=WxH, 쉼표로. 없으면 None."""
    if not text:
        return None
    out: dict[str, list[tuple[int, int]]] = {}
    for item in (x.strip() for x in text.split(",") if x.strip()):
        fmt, _, wh = item.rpartition("=")
        try:
            w, h = (int(v) for v in wh.lower().split("x"))
        except ValueError:
            raise TrainError(f"--synthetic-geometry 의 항목은 WxH 또는 형식=WxH: {item!r}") from None
        if w <= 0 or h <= 0:
            raise TrainError(f"--synthetic-geometry 의 크기는 양수: {item!r}")
        if fmt and fmt not in FORMATS:
            raise TrainError(f"--synthetic-geometry 의 형식은 {', '.join(FORMATS)} 중 하나: {fmt!r}")
        for f in ([fmt] if fmt else FORMATS):
            out.setdefault(f, []).append((w, h))
    return out


def _geometry_of(samples: list[data.Sample], fallback: dict, k: int = 6) -> dict[str, list[tuple[int, int]]]:
    """형식마다 실제 칸 크기(bbox) 중 많은 것 k 개. 실제 칸이 없는 형식은 fallback (--synthetic-geometry 또는 기본)."""
    out = {}
    for f in FORMATS:
        c = Counter((s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1]) for s in samples if s.bbox and s.fmt == f)
        out[f] = [g for g, _n in c.most_common(k)] or list(fallback.get(f, []))
    return {f: v for f, v in out.items() if v}


# ── 실제 칸 → 띠 ───────────────────────────────────────────────────────────
def bands_of(samples: list[data.Sample]) -> dict:
    imgs, left, right = [], [], []
    for s in samples:
        b = band(s.image(), s.cell, s.scale)
        imgs.append(b.image)
        left.append(b.left)
        right.append(b.right)
    return {"images": np.stack(imgs) if imgs else np.zeros((0, INPUT_H, INPUT_W), np.uint8),
            "left": np.array(left, np.float32), "right": np.array(right, np.float32)}


def ink_stats(images: np.ndarray, chunk: int = 1000) -> tuple[np.ndarray, np.ndarray]:
    """(N, H, W) uint8 → 띠마다 (종이색 = 중앙값, 대비 = max(종이색 − 1 백분위, 48)) — model.ink_channel 의 통계. 학습은 띠마다 한 번만
    잰다 (걸음마다 재면 배치 32 의 걸음에 16 ms — 걸음의 8 %)."""
    paper, scale = [], []
    for k in range(0, len(images), chunk):
        flat = images[k:k + chunk].reshape(len(images[k:k + chunk]), -1).astype(np.float32)
        p = np.median(flat, axis=1)
        paper.append(p)
        scale.append(np.maximum(p - np.percentile(flat, 1, axis=1, method="linear"), 48.0))
    if not paper:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    return np.concatenate(paper), np.concatenate(scale)


def ink_batch(images: np.ndarray, stats: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
    """(B, H, W) uint8 → 잉크 채널 (B, H, W) float32 — model.ink_channel 을 배치로 (같은 식). stats: ink_stats 로 미리 잰 것."""
    paper, scale = stats if stats is not None else ink_stats(images)
    g = images.astype(np.float32)
    return np.clip((paper[:, None, None] - g) / scale[:, None, None], -0.3, 1.2).astype(np.float32)


def x_batch(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """(B,) 칸의 왼쪽·오른쪽 → 가로 위치 채널 (B, H, W) — model.x_channel 과 같은 식."""
    cols = np.arange(INPUT_W, dtype=np.float32) + 0.5
    span = np.maximum(right - left, 1.0)
    row = -1.0 + 2.0 * (cols[None, :] - left[:, None]) / span[:, None]
    return np.repeat(row[:, None, :], INPUT_H, axis=1).astype(np.float32)


def inputs(images: np.ndarray, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """띠들 → 모델 입력 (N, 3, H, W) (변형 없이 — 검증·내보내기 확인)."""
    ys = np.repeat(np.linspace(-1, 1, INPUT_H, dtype=np.float32)[:, None], INPUT_W, axis=1)
    return np.stack([ink_batch(images), np.broadcast_to(ys, images.shape).astype(np.float32), x_batch(left, right)], 1) \
        if len(images) else np.zeros((0, N_INPUT_CHANNELS, INPUT_H, INPUT_W), np.float32)


# ── 학습 ──────────────────────────────────────────────────────────────────
def _greedy(logits: np.ndarray) -> str:
    best = logits.argmax(axis=0)
    out, prev = [], 0
    for c in best:
        if c != prev and c != 0:
            out.append(CHARS[c - 1])
        prev = c
    return "".join(out)


def _hit(text: str, targets: list[str]) -> bool:
    """학습 중 검증: 가장 높은 경로를 접은 글자가 정답의 표기 중 하나인가 (거절이면 "?" 가 섞였는가)."""
    if targets == [REJECT]:
        return REJECT in text
    return text in targets


def fit(torch, args: ReadingTrainArgs, rng: np.random.Generator, real: dict, real_t: list[list[str]], syn: dict,
        syn_t: list[list[str]], va_x: np.ndarray, va_t: list[list[str]], progress=_log):
    """CTC 학습 한 번 (digits/train.fit 과 같은 틀): 배치마다 실제:합성 = 1:1, 약한 변형, eval_every 스텝마다 검증 칸으로 정확도·
    손실을 재고 가장 좋은 스텝의 가중치를 남긴다. 목표가 여럿인 칸은 표기마다 CTC 를 계산해 log-sum-exp (4.4)."""
    torch.manual_seed(args.seed)
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    gen = torch.Generator().manual_seed(args.seed)
    ys = torch.linspace(-1, 1, INPUT_H).view(1, 1, INPUT_H, 1).expand(1, 1, INPUT_H, INPUT_W)
    net = build_net(torch, args.channels, SHAPE).to(memory_format=torch.channels_last)
    n_params = int(sum(p.numel() for p in net.parameters()))
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=args.steps, pct_start=0.15)
    ctc = torch.nn.CTCLoss(blank=0, reduction="none", zero_infinity=True)
    va_pairs = calib.pairs_of(va_t)
    log_lines: list[dict] = []
    best = {"acc": -1.0, "loss": float("inf"), "step": 0, "state": None}
    t_train = time.time()
    run_loss = 0.0
    n_real, n_syn = len(real["images"]), len(syn["images"])
    stats = {id(real): ink_stats(real["images"]), id(syn): ink_stats(syn["images"])}
    for step in range(1, args.steps + 1):
        net.train()
        if n_real and n_syn:
            n_r = max(1, int(round(args.batch * args.real_share)))
            pick = [(real, real_t, i) for i in rng.integers(0, n_real, n_r)] + \
                   [(syn, syn_t, i) for i in rng.integers(0, n_syn, args.batch - n_r)]
        else:
            pool, pool_t, n = (real, real_t, n_real) if n_real else (syn, syn_t, n_syn)
            pick = [(pool, pool_t, i) for i in rng.integers(0, n, args.batch)]
        imgs = np.stack([p["images"][i] for p, _t, i in pick])
        left = np.array([p["left"][i] for p, _t, i in pick], np.float32)
        right = np.array([p["right"][i] for p, _t, i in pick], np.float32)
        targets = [t[i] for _p, t, i in pick]
        st = (np.array([stats[id(p)][0][i] for p, _t, i in pick], np.float32),
              np.array([stats[id(p)][1][i] for p, _t, i in pick], np.float32))
        ink = augment_batch(torch, torch.from_numpy(ink_batch(imgs, st))[:, None], gen)
        xb = torch.cat([ink, ys.expand(len(pick), -1, -1, -1), torch.from_numpy(x_batch(left, right))[:, None]], 1)
        xb = xb.contiguous(memory_format=torch.channels_last)
        logp = net(xb).squeeze(2).permute(2, 0, 1).log_softmax(2)                     # (T, B, C)
        loss = multi_target_loss(torch, ctc, logp, targets)
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(net.parameters(), 5.0)
        opt.step()
        sched.step()
        run_loss = 0.9 * run_loss + 0.1 * loss.item() if step > 1 else loss.item()
        if step % args.eval_every == 0 or step == args.steps:
            lg = _torch_logits(torch, net, va_x, shape=SHAPE)
            acc = float(np.mean([_hit(_greedy(z), t) for z, t in zip(lg, va_t, strict=True)])) if len(va_t) else 0.0
            vloss = calib.mean_nll(lg, va_pairs, 1.0) if len(va_t) else 0.0
            log_lines.append({"step": step, "loss": round(run_loss, 5), "lr": round(sched.get_last_lr()[0], 6),
                              "val_acc": round(acc, 5), "val_loss": round(vloss, 5), "seconds": round(time.time() - t_train, 1)})
            progress(f"[{step}/{args.steps}] 손실 {run_loss:.4f} · 검증 정확도 {acc:.4f} · 검증 손실 {vloss:.4f} · "
                     f"{time.time() - t_train:.0f}s")
            if (acc, -vloss) > (best["acc"], -best["loss"]):
                best = {"acc": acc, "loss": vloss, "step": step,
                        "state": {k: v.detach().clone() for k, v in net.state_dict().items()}}
    train_seconds = time.time() - t_train
    if best["state"] is not None:
        net.load_state_dict(best["state"])
    net.eval()
    return net, best, log_lines, train_seconds, n_params


def multi_target_loss(torch, ctc, logp, targets: list[list[str]]):
    """(T, B, C) 로그 확률과 칸마다의 표기들 → 평균 −log Σ P(표기). (칸, 표기) 쌍을 펼쳐 CTC(reduction='none') 한 번, 칸마다 log-sum-exp."""
    owner, flat, lens = [], [], []
    for b, ts in enumerate(targets):
        for t in ts:
            owner.append(b)
            y = encode(t)
            flat += y
            lens.append(len(y))
    own = torch.tensor(owner, dtype=torch.long)
    lp = logp[:, own, :]
    il = torch.full((len(owner),), logp.shape[0], dtype=torch.long)
    nll = ctc(lp, torch.tensor(flat, dtype=torch.long), il, torch.tensor(lens, dtype=torch.long))      # (P,)
    k = max(len(ts) for ts in targets)
    slot = torch.tensor([j for ts in targets for j in range(len(ts))], dtype=torch.long)
    m = torch.full((len(targets), k), float("inf"))
    m = m.index_put((own, slot), nll)
    return (-torch.logsumexp(-m, dim=1)).mean()


# ── 기준을 정하는 읽기 ────────────────────────────────────────────────────────
def cv_folds(dates: list[str], salt: str, k: int) -> dict[str, int]:
    """날짜 → 묶음 번호 (sha256(salt:cv:날짜) 순서로 돌아가며 — meta/train.cv_folds 와 같은 규칙)."""
    order = sorted(set(dates), key=lambda d: hashlib.sha256(f"{salt}:cv:{d}".encode()).hexdigest())
    return {d: i % k for i, d in enumerate(order)}


def _read(onnx: OnnxNet, x: np.ndarray, fmts: list[str], truths: list[str], temperature: float) -> list[calib.Read]:
    lg = onnx.logits_many(x)
    return calib.read_all(list(lg), fmts, truths, temperature)


def write_cv_reads(path: Path, reads: list[calib.Read], folds: list[int]) -> None:
    """--cv 의 읽기(묶음마다의 모델로 읽은 것)를 모델 폴더에 남긴다 — recognizer eval --split val 이 쓴다 (묶음마다의 모델은 남기지
    않으므로). 값은 적지 않는다: 판정 줄(calib.flags — 형식, 정답·답의 종류, 신뢰도, 맞았는가 …)과 정답의 모양(dddd.d)."""
    with open(path, "w", encoding="utf-8") as f:
        for r, row, fold in zip(reads, calib.flags(reads), folds, strict=True):
            row = {"fold": fold, **row, "shape": shape_of(r.truth) if r.truth not in ("", REJECT) else ""}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_cv_reads(path: Path) -> list[dict]:
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


# ── 한 번에 ────────────────────────────────────────────────────────────────
def train(crops_dir: str | Path | None, out_dir: str | Path, args: ReadingTrainArgs, *, split_salt: str = "synthetic",
          allow_in_repo: bool = False, progress=_log) -> dict:
    """학습하고 out_dir 에 model.onnx, card.json, train-log.jsonl (--cv 면 cv-reads.jsonl 도) 을 쓴다. 돌려주는 값: 카드."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise TrainError(f"같은 이름의 모델이 있습니다: {out_dir} — 덮어쓰지 않습니다. 다른 --name 을 주세요")
    clean_orphan_staging(out_dir.parent)
    if inside_git_tree(out_dir.parent if not out_dir.exists() else out_dir) and not allow_in_repo:
        raise TrainError(f"{out_dir} 은 git 작업 트리 안입니다. 현장 글씨로 학습한 모델은 사이트 팩에 둡니다 "
                         "(합성 칸만으로 만든 시험용 모델이면 --allow-in-repo)")
    if not 0 <= args.val_share < 1:
        raise TrainError(f"--val-share 는 0 이상 1 미만: {args.val_share}")
    if not 0 < args.target_auto_error < 1:
        raise TrainError(f"--target-auto-error 는 0 초과 1 미만: {args.target_auto_error}")
    if args.cv is not None and args.cv < 2:
        raise TrainError(f"--cv 는 2 이상: {args.cv}")
    t_start = time.time()
    rng = np.random.default_rng([args.seed, 99])

    crops = None
    samples: list[data.Sample] = []
    if crops_dir is not None:
        try:
            crops = data.read_crops(crops_dir)
        except digits_data.CropsError as e:
            raise TrainError(str(e)) from e
        samples = crops.samples
    if args.cv and not samples:
        raise TrainError("--cv 에는 실제 칸(--crops)이 필요합니다 — 합성만으로 학습하면 합성 검증 칸으로 기준을 정합니다")
    check_torch()
    spec = crops.spec if crops and crops.spec else DEFAULT_SPEC
    salt = digits_data.val_salt(split_salt)
    if args.cv:
        cv_salt = f"{split_salt}:cv"
        fold_of = cv_folds([s.work_date for s in samples if s.work_date], cv_salt, args.cv)
        tr_all, va_all = samples, []
    else:
        tr_all, va_all = digits_data.split_val(samples, salt, args.val_share)    # 같은 날짜는 한쪽에만 (Sample.work_date)
    va = [s for s in va_all if s.inked]                     # 검증은 인식기에 가는 칸만 (digits 와 같은 까닭)
    fallback = args.geometry or DEFAULT_GEOMETRY
    geometry = _geometry_of(samples, fallback) if samples else {f: list(v) for f, v in fallback.items() if v}
    progress(f"실제 칸: 학습 {len(tr_all)} (날짜 {len({s.work_date for s in tr_all})}), 검증 {len(va)} · 규격 {spec.describe()} · "
             f"형식 {', '.join(f'{f} {sum(s.fmt == f for s in samples)}' for f in FORMATS)}")

    n_syn = args.synthetic_n(len(tr_all))
    t0 = time.time()
    syn = synth_pool(n_syn, args.seed, geometry, spec, args.workers) if n_syn else synth_pool(0, args.seed, geometry, spec, 1)
    syn_t = [[t] for t in syn["texts"]]
    progress(f"합성 칸 {len(syn_t)}개 ({'; '.join(f'{f} ' + ', '.join(f'{w}x{h}' for w, h in g) for f, g in geometry.items())})"
             f" — {time.time() - t0:.1f}s")
    if not tr_all and not syn_t:
        raise TrainError("학습할 칸이 없습니다 (실제 칸 0, --synthetic 0)")
    torch = require_torch()
    _check_export(torch, args.channels, SHAPE)

    def real_pool(ss):
        return bands_of(ss), [s.targets for s in ss]

    val_source = "real"
    if args.cv:
        val_source = f"cv{args.cv}"
    elif not va:
        val_source = "synthetic"
    syn_va = None
    if val_source == "synthetic":
        syn_va = synth_pool(SYNTH_VAL, args.seed + 7_777, geometry, spec, args.workers)
        keep = [i for i, k in enumerate(syn_va["kinds"]) if k != "blank"]          # 잉크가 전혀 없는 칸은 인식기에 가지 않는다
        syn_va = {k: (v[keep] if isinstance(v, np.ndarray) else [v[i] for i in keep]) for k, v in syn_va.items()}

    tmp = staging_dir(out_dir)
    try:
        reads: list[calib.Read] = []
        cv_info = None
        if args.cv:                                          # 묶음마다 나머지로 학습해 그 묶음을 읽는다 (온도 1 로 — 보정은 모은 뒤에)
            logits_all, fmts_all, truths_all, targets_all, folds_all = [], [], [], [], []
            for f in range(args.cv):
                tr = [s for s in samples if fold_of.get(s.work_date or "", -1) != f]
                rd = [s for s in samples if fold_of.get(s.work_date or "", -1) == f and s.inked]
                if not rd:
                    continue
                r_pool, r_t = real_pool(tr)
                progress(f"묶음 {f + 1}/{args.cv}: 학습 {len(tr)} · 읽기 {len(rd)}")
                net_f, *_ = fit(torch, args, rng, r_pool, r_t, syn, syn_t, inputs(**bands_of(rd)), [s.targets for s in rd],
                                progress)
                export_onnx(torch, net_f, tmp / "fold.onnx", SHAPE)
                lg = OnnxNet(tmp / "fold.onnx", SHAPE.n_classes).logits_many(inputs(**bands_of(rd)))
                logits_all += list(lg)
                fmts_all += [s.fmt for s in rd]
                truths_all += [s.value for s in rd]
                targets_all += [s.targets for s in rd]
                folds_all += [f] * len(rd)
            (tmp / "fold.onnx").unlink(missing_ok=True)
            va_x = inputs(**bands_of([s for s in samples if s.inked][:256]))     # 최종 학습의 진행 표시용 (기준과 무관)
            va_t = [s.targets for s in samples if s.inked][:256]
            cv_info = {"k": args.cv, "salt": cv_salt, "folds": dict(Counter(fold_of.values())),
                       "rule": "train 날짜를 sha256(salt:cv:날짜) 순서로 돌아가며 K 묶음에 나눈다; 묶음마다 나머지로 학습해 그 묶음을 읽는다"}
        elif syn_va is not None:
            va_x = inputs(syn_va["images"], syn_va["left"], syn_va["right"])
            va_t = [[t] for t in syn_va["texts"]]
        else:
            va_x = inputs(**bands_of(va))
            va_t = [s.targets for s in va]
        r_pool, r_t = real_pool(tr_all)
        net, best, log_lines, train_seconds, n_params = fit(torch, args, rng, r_pool, r_t, syn, syn_t, va_x, va_t, progress)
        exporter = export_onnx(torch, net, tmp / "model.onnx", SHAPE)
        onnx_net = OnnxNet(tmp / "model.onnx", SHAPE.n_classes)

        # 검증: 최종 모델로 검증 칸을 (--cv 면 묶음 읽기를) 읽어 온도·기준
        if args.cv:
            cal_logits, cal_fmts, cal_truths, cal_targets = logits_all, fmts_all, truths_all, targets_all
        elif syn_va is not None:
            cal_logits = list(onnx_net.logits_many(va_x))
            cal_fmts = list(syn_va["fmts"])
            cal_truths = [_truth_of(f, t) for f, t in zip(syn_va["fmts"], syn_va["texts"], strict=True)]
            cal_targets = va_t
        else:
            cal_logits = list(onnx_net.logits_many(va_x))
            cal_fmts, cal_truths, cal_targets = [s.fmt for s in va], [s.value for s in va], va_t
        th_logits = _torch_logits(torch, net, va_x[:64], shape=SHAPE)
        cv_logits = onnx_net.logits_many(va_x[:64])
        diff = float(np.abs(cv_logits - th_logits).max()) if len(th_logits) else 0.0
        same = all(_greedy(a) == _greedy(b) for a, b in zip(cv_logits, th_logits, strict=True))
        t_cal = time.time()
        temp, tinfo = calib.fit_temperature(cal_logits, cal_targets)
        tinfo["seconds"] = round(time.time() - t_cal, 2)
        reads = calib.read_all(cal_logits, cal_fmts, cal_truths, temp)
        table = calib.threshold_table(reads)
        chosen = calib.choose_threshold(table, args.target_auto_error, args.min_val_auto)
        reason = None if chosen else calib.why_no_threshold(table, args.target_auto_error, args.min_val_auto)
        if tr_all and val_source == "synthetic":
            chosen, reason = None, "실제 검증 날짜의 칸이 없어 기준을 정하지 않았다 (합성 칸의 수치는 참고만)"
        if args.cv:
            write_cv_reads(tmp / CV_READS, reads, folds_all)
        val_score = calib.score(reads, None if chosen is None else chosen["threshold"])

        card = {
            "name": args.name,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "code_version": code_version(),
            "reader": READER,
            "formats": list(FORMATS),
            "spec": spec.to_dict(),
            "input": {"width": INPUT_W, "height": INPUT_H, "channels": N_INPUT_CHANNELS, "band": dict(BAND),
                      "preprocess": "recognize/readings/model.py: band → channels (잉크, 세로 위치(띠), 가로 위치(칸))"},
            "chars": CHARS,
            "architecture": {"layers": describe_net(args.channels, SHAPE), "channels": list(args.channels), "params": n_params,
                             "onnx_bytes": (tmp / "model.onnx").stat().st_size, "exporter": exporter, "batch": 1,
                             "beam": BEAM},
            "train_args": {k: v for k, v in asdict(args).items() if k not in ("name", "seed", "workers", "geometry")}
            | {"synthetic": len(syn_t), "geometry": {f: [list(g) for g in v] for f, v in geometry.items()},
               "augment": "이동 ±4%/±6%, 크기 0.9–1.1, 회전 ±3°, 기울임 ±0.12, 대비 0.7–1.2, 밝기 ±0.1, 흐림·잡음 30%; 뒤집기 없음",
               "loss": "−log Σ P(표기) — 실제 칸은 formats.spellings 의 표기 모두, 합성 칸은 적힌 글자 하나",
               "mix": "실제:합성 = 1:1 (배치 기준)" if tr_all and syn_t else ("합성만" if syn_t else "실제만")},
            "seed": args.seed,
            "data": {
                "crops": None if crops is None else {"lines": crops.lines, "skipped": crops.skipped, "splits": sorted(crops.splits)},
                "train": data.summarize(tr_all), "val": data.summarize(va_all),
                "synthetic": {"cells": len(syn_t), "geometry": {f: [list(g) for g in v] for f, v in geometry.items()},
                              "val_cells": len(syn_va["texts"]) if syn_va is not None else 0,
                              "kinds": dict(sorted(Counter(syn["kinds"]).items()))},
                "val_rule": {"salt": salt, "share": args.val_share,
                             "rule": "sha256(salt:날짜) 앞 32비트 / 2^32 < share 이면 검증 (evaluate/split.py)"},
            },
            "validation": {"source": val_source, "method": "cv" if args.cv else "val_dates", "cv": cv_info,
                           "cells": len(reads), "excluded_no_ink": len(va_all) - len(va), "best_step": best["step"],
                           "score": val_score, "train_seconds": round(train_seconds, 1), "total_seconds": None,
                           "export_check": {"max_abs_diff": diff, "same_answers": bool(same), "cells": int(len(th_logits))},
                           "calibration": tinfo},
            "temperature": temp,
            "auto_accept": {"target": args.target_auto_error, "threshold": None if chosen is None else chosen["threshold"],
                            "met": chosen is not None, "reason": reason, "min_auto": args.min_val_auto,
                            "auto": None if chosen is None else chosen["auto"],
                            "errors": None if chosen is None else chosen["errors"],
                            "upper95": None if chosen is None else chosen["error_ci95"][1],
                            "basis": f"검증 칸 ({val_source})",
                            "rule": "오류율(자동 적재된 칸 중 정답과 다른 비율)이 목표 이하인 가장 낮은 임계값. 대상 = 값(점으로 쓴 "
                                    "시각이 아닌 것)과 빈 칸, 맞았는가 = formats.same_value (tasks/0011 4.3)",
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
            out_dir.rmdir()
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(out_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    thr = (f"{chosen['threshold']} (자동 적재 {chosen['auto']}칸 중 오류 {chosen['errors']}, 95 % 상한 {chosen['error_ci95'][1]:.1%})"
           if chosen else f"없음 ({reason})")
    progress(f"모델: {out_dir} · 검증 {val_source} {len(reads)}칸 · 온도 {temp:g} · 자동 적재 기준 {thr} · "
             f"{time.time() - t_start:.0f}s")
    return card


def _truth_of(fmt: str, text: str) -> str:
    """합성 칸의 적힌 글자 → 정답 (정규화한 값 | "" | "?"). 점으로 쓴 시각은 사람이 넣는 값으로 (시각 — 1절 라)."""
    from ...forms.formats import dotted_clock, normalize

    if text in ("", REJECT):
        return text
    if fmt == "reading" and dotted_clock(text) is not None:
        return dotted_clock(text)
    return normalize(fmt, text)
