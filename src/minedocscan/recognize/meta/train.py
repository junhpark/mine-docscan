"""메타 필드 모델 학습 — torch 는 여기와 digits/train.py, choice/train.py 에서만 (tasks/0004 단계 3·4).

  minedocscan recognizer train --crops DIR --meta-key vehicle_no --name veh-v1 [--cv 5] [--reader digits|choice]

1. 데이터: `review export-crops --meta` 의 폴더에서 그 키의 줄만 (test 줄이 있으면 거절, 규격 하나). 읽는 법은 정답이 전부
   숫자열이면 digits, 아니면 choice (--reader 로 바꾼다).
2. 기준을 정할 읽기:
   · --cv K: train 날짜를 K 묶음으로 나눠(날짜 단위, 소금값으로 고정) 묶음마다 "나머지로 학습 → 그 묶음을 읽기". 그렇게 모은
     읽기 **전체**에서 온도와 기준을 정한다. 내보내는 모델은 train 날짜 전부로 학습한 것이다 (4.5).
   · --cv 가 없으면 0003 과 같이 검증 날짜(train 날짜 안에서 날짜 단위로 뗀 몫)를 그 날짜 없이 학습한 모델로 읽는다.
   묶음·검증의 후보 목록은 그 읽기를 한 모델이 학습 때 본 값 + 템플릿 값 — 그래서 학습에 없던 값(새 차)도 검증에 나온다.
3. 학습한 망은 ONNX 로 내보내고 그 파일을 OpenCV 로 다시 읽어 검증한다 (운용과 같은 경로).
4. <site>/models/NAME/: model.onnx, card.json, classes.json, train-log.jsonl. 카드·로그·표준 출력에 값(이름·차량번호)을
   적지 않는다 — 종류의 수와 종류별 개수의 분포만 (4.7). 값은 classes.json 에만 있다.

숫자 모델(digits): 0003 의 망·학습(digits/train.fit)을 그대로 쓰고, 합성 셀은 필드 모양의 합성 크롭(tools/synth_meta)이다.
분류기(choice): recognize/choice/train.py.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import shutil
import statistics
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

from ...forms.template import DATE_PARTS
from ...imaging.cropspec import CropSpec
from ...review.export import inside_git_tree
from ...tools import synth_meta
from ..digits import data
from ..digits.model import clean_orphan_staging, staging_dir
from ..digits.train import TrainArgs, TrainError, _log, check_torch, code_version
from . import calib
from .choose import UNLISTED_RATIO, choose, readable, truth_prob
from .model import FIXED_VALUES

META_SPEC = synth_meta.META_SPEC
ASCII_DIGITS = re.compile(r"[0-9]+")          # str.isdigit 는 '²' 같은 글자도 받는다
SYNTH_VAL = 400                     # 학습 중 가장 좋은 스텝을 고르는 합성 검증 크롭 수 (기준은 실제 읽기로 정한다)
DEFAULT_TARGET = 0.02               # 메타 필드의 자동 적재 오류율 목표 (tasks/0004 9절 — 과제 책임자가 정한다)
DEFAULT_STEPS = {"digits": 2500, "choice": 600}


@dataclass
class MetaTrainArgs(TrainArgs):
    keys: tuple[str, ...] = ()
    reader: str | None = None                 # digits | choice | None(정답에서 정한다)
    cv: int | None = None                     # 날짜 묶음 수. None 이면 검증 날짜 (0003 과 같다)
    min_examples: int = 3                     # 분류기의 종류가 되려면 학습 날짜에 있어야 하는 예의 수
    target_auto_error: float = DEFAULT_TARGET
    template_values: dict = field(default_factory=dict)      # 키 → 템플릿에 인쇄된 값 (후보 목록에 더한다)
    # 위치 채널(digits/model.normalize)을 끈다. 숫자 칸 모델은 "가운데 칸의 글씨인가"를 가르는 데 쓰지만, 넓은 메타 필드에서는
    # 그것이 지름길이 되어 학습이 엉뚱한 자리에 머물렀다 — 같은 합성 월·일 1,000 스텝에서 위치 채널이 있으면 검증 정확도 0.27–0.37,
    # 없으면 1.00 (마지막 자리를 늘 틀리던 차량번호도 같은 까닭이었다)
    position: bool = False
    # 배치 중 실제 크롭의 몫. 실제 크롭은 값이 몇 가지뿐이고(차가 열한 대) 같은 차는 같은 사람이 쓴다 — 아무 번호나 쓴 합성 크롭이
    # 대부분이어야 숫자를 읽는다 (글씨체로 번호를 외우지 않게)
    real_share: float = 0.2
    # --extra-digits: 숫자 칸(운반 횟수)의 export-crops 폴더. 그 칸의 숫자열을 CTC 학습에만 더한다 (후보·기준·읽기에는 쓰지 않는다).
    # 실제 일보에서 합성 숫자는 0.99 로 읽는데 실제 차량번호는 20쪽 중 7쪽만 맞았다 — 실제 숫자를 본 적이 거의 없어서 (tasks/0005 단계 1)
    extra_digits: str | None = None

    def synthetic_n(self, n_real: int) -> int:
        return int(self.synthetic) if self.synthetic is not None else 3000


# ── 데이터 ─────────────────────────────────────────────────────────────────
def read_meta(crops_dir, keys: tuple[str, ...]) -> data.Crops:
    try:
        crops = data.read_crops(crops_dir, meta_keys=keys)
    except data.CropsError as e:
        raise TrainError(str(e)) from e
    if not crops.samples:
        raise TrainError(f"이 폴더에 {', '.join(keys)} 의 메타 필드 줄이 없습니다 — `minedocscan review export-crops --meta` 로 "
                         "내보낸 폴더를 주세요")
    return crops


def decide_reader(samples: list[data.Sample], requested: str | None) -> str:
    """정답이 전부 숫자열이면 digits, 아니면 choice."""
    vals = [s.text for s in samples if s.verdict == "value"]
    auto = "digits" if vals and all(v.isdigit() for v in vals) else "choice"
    if requested is None:
        return auto
    if requested not in ("digits", "choice"):
        raise TrainError(f"--reader 는 digits | choice: {requested}")
    if requested == "digits" and auto != "digits":
        raise TrainError(f"--reader digits 인데 숫자가 아닌 정답이 있습니다 ({sum(not v.isdigit() for v in vals)}개) — "
                         "이름 같은 필드는 --reader choice")
    return requested


def cv_folds(dates: list[str], salt: str, k: int) -> dict[str, int]:
    """날짜 → 묶음 번호. hash(소금값, 날짜) 순서로 돌아가며 나눈다 — 묶음 크기가 고르고, 같은 날짜·소금값이면 언제나 같다."""
    order = sorted(set(dates), key=lambda d: hashlib.sha256(f"{salt}:cv:{d}".encode()).hexdigest())
    return {d: i % k for i, d in enumerate(order)}


def count_summary(samples: list[data.Sample]) -> dict:
    """카드에 적는 요약 — 값 없이: 칸 수, 날짜 수, 종류 수, 종류별 개수의 최소·중앙·최대."""
    c = Counter(s.text for s in samples if s.verdict == "value")
    n = sorted(c.values())
    return {"cells": len(samples), "dates": len({s.work_date for s in samples if s.work_date}), "classes": len(c),
            "per_class": {"min": n[0], "median": statistics.median(n), "max": n[-1]} if n else None,
            "by_verdict": dict(sorted(Counter(s.verdict for s in samples).items()))}


def _geometry(samples: list[data.Sample], key: str) -> tuple[int, int]:
    c = Counter((s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1]) for s in samples if s.bbox and s.meta_key == key)
    return c.most_common(1)[0][0] if c else synth_meta.DEFAULT_BOX.get(key, (390, 65))


# ── 숫자 모델의 합성 크롭 ──────────────────────────────────────────────────
def _gen_meta_chunk(job) -> list[tuple[np.ndarray, str]]:
    from ..digits.model import resize_input

    seed, key, geom, spec_d, idx = job
    cv2.setNumThreads(1)
    spec = CropSpec.from_dict(spec_d)
    fs = synth_meta.FieldSpec(int(geom[0]), int(geom[1]), spec)
    out = []
    for i in idx:
        rng = np.random.default_rng([seed, i])
        text = synth_meta.random_text(rng, "vehicle_no" if key == NUMBERS else key)
        img = synth_meta.make_field_crop(rng, text, "digits", fs)
        # 날짜의 부분은 0 을 붙여 써도("07") 정답은 앞의 0 을 뗀 값 — 실제 크롭의 정답(쪽의 날짜)과 같은 표기로 가르친다
        out.append((resize_input(img), str(int(text)) if key in DATE_PARTS else text))
    return out


NUMBERS = "_numbers"                # 합성 풀의 "아무 숫자열" (키와 상관없이 늘 넣는다)
NUMBERS_BOX = (390, 65)


def meta_synth_pool(n: int, seed: int, geoms: dict[str, tuple[int, int]], spec: CropSpec,
                    workers: int | None = None, chunk: int = 200) -> tuple[list[np.ndarray], list[str]]:
    """키마다 고르게 합성 필드 크롭 n 개 (모델 입력 크기로 줄인 것). 같은 인자면 같은 크롭.
    차량번호 키가 없어도 "아무 숫자열"(1–6자리, 넓은 필드)을 한 몫 넣는다 — 월·일만이면 한두 자리 숫자만 보게 된다."""
    geoms = dict(geoms)
    if NUMBERS not in geoms and "vehicle_no" not in geoms:
        geoms[NUMBERS] = NUMBERS_BOX
    keys = sorted(geoms)
    jobs = []
    for g, key in enumerate(keys):
        n_k = n // len(keys) + (1 if g < n % len(keys) else 0)
        seed_k = seed * 100 + g
        idx = list(range(n_k))
        jobs += [(seed_k, key, geoms[key], spec.to_dict(), idx[j:j + chunk]) for j in range(0, n_k, chunk)]
    workers = max(1, min(workers or os.cpu_count() or 1, len(jobs)))
    if workers == 1:
        parts = [_gen_meta_chunk(j) for j in jobs]
    else:                                                    # spawn: digits/train.synth_pool 와 같은 이유
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as ex:
            parts = list(ex.map(_gen_meta_chunk, jobs))
    flat = [x for p in parts for x in p]
    return [x[0] for x in flat], [x[1] for x in flat]


# ── 학습 ──────────────────────────────────────────────────────────────────
def train_meta(crops_dir, out_dir, args: MetaTrainArgs, *, split_salt: str = "synthetic", allow_in_repo: bool = False,
               progress=_log) -> dict:
    """메타 필드 모델을 학습하고 out_dir 에 쓴다. 돌려주는 값: 카드."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise TrainError(f"같은 이름의 모델이 있습니다: {out_dir} — 덮어쓰지 않습니다. 다른 --name 을 주세요")
    clean_orphan_staging(out_dir.parent)                      # 끊긴 학습이 남긴 주인 없는 임시 폴더 (시작할 때 치운다)
    if inside_git_tree(out_dir.parent if not out_dir.exists() else out_dir) and not allow_in_repo:
        raise TrainError(f"{out_dir} 은 git 작업 트리 안입니다. 현장 글씨(이름·차량번호)로 학습한 모델은 사이트 팩에 둡니다 "
                         "(합성 데이터만으로 만든 시험용 모델이면 --allow-in-repo)")
    if not args.keys:
        raise TrainError("--meta-key 가 필요합니다")
    if args.cv is not None and args.cv < 2:
        raise TrainError(f"--cv 는 2 이상: {args.cv}")
    if not 0 < args.target_auto_error < 1:
        raise TrainError(f"--target-auto-error 는 0 초과 1 미만: {args.target_auto_error}")
    crops = read_meta(crops_dir, tuple(args.keys))
    reader = decide_reader(crops.samples, args.reader)
    if args.steps is None:                                    # 읽는 법마다의 기본 스텝
        args.steps = DEFAULT_STEPS[reader]
    if reader == "choice" and len(args.keys) != 1:
        raise TrainError("분류기(choice)는 키 하나만 (--meta-key operator)")
    if args.extra_digits and reader != "digits":
        raise TrainError("--extra-digits 는 숫자 모델(digits — 차량번호·월·일)에만 씁니다")
    if args.extra_digits:                                     # 데이터의 오류를 torch 의 오류보다 먼저 (0003 의 학습과 같은 순서)
        read_extra_digits(args.extra_digits, crops.spec or META_SPEC)
    check_torch()
    if reader == "digits":
        return _train_digits(crops, out_dir, args, split_salt, progress)
    from ..choice.train import train_choice

    return train_choice(crops, out_dir, args, split_salt=split_salt, progress=progress)


def read_extra_digits(crops_dir, spec: CropSpec) -> tuple[list[data.Sample], dict]:
    """--extra-digits: 0003 의 export-crops(숫자 칸) 폴더 → 숫자열이 정답인 칸 (빈 칸·읽을 수 없음은 뺀다)과 카드에 적을 요약.
    test 줄이 있으면 거절(학습에 쓰지 않는다 — ADR 0009), 크롭 규격이 메타 필드 크롭과 다르면 거절(같은 모델의 입력이 하나여야 한다)."""
    try:
        extra = data.read_crops(crops_dir)                    # 표의 칸만 (메타 필드 줄은 뺀다), test 가 있으면 CropsError
    except data.CropsError as e:
        raise TrainError(f"--extra-digits: {e}") from e
    if extra.spec is not None and extra.spec != spec:
        pad = "" if spec.pad is None else f" --pad {spec.pad}"
        raise TrainError(f"--extra-digits 의 크롭 규격({extra.spec.describe()})이 메타 필드 크롭의 규격({spec.describe()})과 "
                         f"다릅니다 — 같은 규격으로 다시 내보내세요: review export-crops DIR --kind handwritten_number "
                         f"--split train --res {spec.res} --scale {spec.scale:g}{pad}")
    digits = [s for s in extra.samples if ASCII_DIGITS.fullmatch(s.text)]
    return digits, {"cells": len(digits), "dates": len({s.work_date for s in digits if s.work_date}),
                    "skipped": len(extra.samples) - len(digits), "spec": spec.describe(),
                    "use": "CTC 학습에만 (후보 목록·온도·기준·읽기에는 쓰지 않는다)"}


def plan_reads(samples: list[data.Sample], args: MetaTrainArgs, split_salt: str) -> tuple[list[tuple[list, list]], dict]:
    """기준을 정할 읽기의 계획: [(학습 셀, 읽을 셀)] 과 카드에 적을 방식. --cv 면 묶음마다, 아니면 검증 날짜 하나."""
    dates = sorted({s.work_date for s in samples if s.work_date})
    if args.cv:
        salt = f"{split_salt}:cv"
        fold = cv_folds(dates, salt, args.cv)
        plans = []
        folds = []
        for f in range(args.cv):
            va = [s for s in samples if s.work_date and fold[s.work_date] == f]
            tr = [s for s in samples if not (s.work_date and fold[s.work_date] == f)]
            fd = sorted({s.work_date for s in va})
            folds.append({"fold": f, "dates": fd, "cells": len(va)})
            if va and tr:
                plans.append((tr, va))
        for i, a in enumerate(folds):                         # 묶음의 날짜는 서로 겹치지 않는다
            for b in folds[i + 1:]:
                assert not set(a["dates"]) & set(b["dates"]), "묶음의 날짜가 겹친다"
        return plans, {"method": f"cv{args.cv}", "k": args.cv, "salt": salt, "folds": folds,
                       "rule": "train 날짜를 sha256(salt:cv:날짜) 순서로 돌아가며 K 묶음에 나눈다; 묶음마다 나머지로 학습해 그 묶음을 읽는다"}
    salt = data.val_salt(split_salt)
    tr, va = data.split_val(samples, salt, args.val_share)
    return ([(tr, va)] if va else []), {"method": "val_dates", "salt": salt, "share": args.val_share,
                                        "dates": sorted({s.work_date for s in va}), "cells": len(va)}


def candidates_for(key: str, train_samples: list[data.Sample], args: MetaTrainArgs) -> list[str]:
    """그 읽기를 한 모델이 고를 후보: 학습 셀에 나온 값 + 템플릿 값 (날짜의 부분은 고정 범위)."""
    if key in FIXED_VALUES:
        return list(FIXED_VALUES[key])
    vals = sorted({s.text for s in train_samples if s.meta_key == key and s.verdict == "value"})
    vals += [v for v in args.template_values.get(key, []) if v not in vals]
    return [v for v in vals if readable(v)]


def _train_digits(crops: data.Crops, out_dir: Path, args: MetaTrainArgs, split_salt: str, progress) -> dict:
    from ..digits.model import (
        CHARS,
        INPUT_H,
        INPUT_W,
        N_INPUT_CHANNELS,
        OnnxNet,
        normalize,
        read_answers,
        resize_input,
        sha256_file,
    )
    from ..digits.train import (
        _check_export,
        _ink,
        _torch_logits,
        describe_net,
        export_onnx,
        fit,
        libraries,
        require_torch,
    )

    t_start = time.time()
    samples = [s for s in crops.samples if s.text.isdigit() or s.text == "?"]
    skipped = len(crops.samples) - len(samples)
    spec = crops.spec or META_SPEC
    geoms = {k: _geometry(samples, k) for k in args.keys}
    plans, method = plan_reads(samples, args, split_salt)
    extra, extra_info = read_extra_digits(args.extra_digits, spec) if args.extra_digits else ([], None)
    if extra_info:
        progress(f"--extra-digits: 숫자 칸 {extra_info['cells']}개 ({extra_info['dates']}일)를 학습에 더한다 — 읽기·기준에는 쓰지 않는다")
        # 배치의 합성 쪽에 섞는다: 실제 쪽(real_share)에 넣으면 수천 개의 운반 칸이 몇 백 개뿐인 메타 크롭을 밀어낸다

    def extra_pool(skip_dates: set) -> tuple[np.ndarray, list[str]]:
        """합성 크롭 + 숫자 칸(읽을 날짜의 것은 뺀다 — 같은 날 같은 사람의 글씨로 학습한 모델이 그날을 읽으면 기준이 부푼다)."""
        ex = [s for s in extra if s.work_date not in skip_dates]
        if not ex:
            return syn_ink, syn_y
        return np.concatenate([syn_ink, _ink([small(s) for s in ex])]), syn_y + [s.text for s in ex]
    progress(f"메타 필드 {', '.join(args.keys)} (숫자 모델): 셀 {len(samples)} (날짜 {len({s.work_date for s in samples})}) · "
             f"규격 {spec.describe()} · 기준을 정하는 읽기: {method['method']} ({len(plans) if args.cv else 0}번 학습 + 최종 1번)")
    n_syn = args.synthetic_n(len(samples))
    t0 = time.time()
    syn_x, syn_y = meta_synth_pool(n_syn, args.seed, geoms, spec, args.workers) if n_syn else ([], [])
    sval_x, sval_y = meta_synth_pool(SYNTH_VAL, args.seed + 7_777, geoms, spec, args.workers)
    progress(f"합성 필드 크롭 {len(syn_x)} + 검증 {len(sval_x)} — {time.time() - t0:.1f}s")
    syn_ink = _ink(syn_x)
    sval_xn = (np.stack([normalize(x, args.position) for x in sval_x]) if sval_x
               else np.zeros((0, 3, INPUT_H, INPUT_W), np.float32))
    cache: dict[str, np.ndarray] = {}

    def small(s: data.Sample) -> np.ndarray:
        k = str(s.path)
        if k not in cache:
            cache[k] = resize_input(s.image())
        return cache[k]

    torch = require_torch()
    _check_export(torch, args.channels)
    tmp = staging_dir(out_dir)                              # 끊긴 학습의 주인 없는 임시 폴더도 여기서 치운다
    try:
        reads: list[calib.Read] = []
        def read_with(onnx, tr, va, f) -> list[calib.Read]:
            cands = {k: candidates_for(k, tr, args) for k in args.keys}
            lg = onnx.logits_many(np.stack([normalize(small(s), args.position) for s in va]))
            return [calib.Read(z, s.meta_key, cands[s.meta_key], s.text, s.work_date, f, {"field_id": s.field_id})
                    for z, s in zip(lg, va, strict=True)]

        if args.cv:                                       # 묶음마다 나머지로 학습해 그 묶음을 읽는다
            for f, (tr, va) in enumerate(plans):
                progress(f"읽기 {f + 1}/{len(plans)}: 학습 {len(tr)}셀 → {len(va)}셀을 읽는다")
                rng = np.random.default_rng([args.seed, 99, f])
                ex_ink, ex_y = extra_pool({s.work_date for s in va})
                net, *_rest = fit(torch, args, rng, _ink([small(s) for s in tr]), [s.text for s in tr], ex_ink, ex_y,
                                  sval_xn, sval_y, progress=lambda *_a: None)
                export_onnx(torch, net, tmp / f"read{f}.onnx")
                reads += read_with(OnnxNet(tmp / f"read{f}.onnx"), tr, va, f)
        # 최종 모델: train 날짜 전부 (--cv) 또는 검증 날짜를 뺀 것 (검증 날짜 방식 — 0003 과 같다: 그 모델로 검증 날짜를 읽는다)
        final_tr = samples if args.cv or not plans else plans[0][0]
        rng = np.random.default_rng([args.seed, 99])
        # 검증 날짜 방식이면 최종 모델이 검증 날짜를 읽는다 — 그 날짜의 숫자 칸은 뺀다. --cv 면 최종 모델은 읽지 않는다
        ex_ink, ex_y = extra_pool(set() if args.cv or not plans else {s.work_date for s in plans[0][1]})
        net, best, log_lines, train_seconds, n_params = fit(torch, args, rng, _ink([small(s) for s in final_tr]),
                                                            [s.text for s in final_tr], ex_ink, ex_y, sval_xn, sval_y, progress)
        exporter = export_onnx(torch, net, tmp / "model.onnx")
        onnx = OnnxNet(tmp / "model.onnx")
        if not args.cv and plans:
            reads = read_with(onnx, plans[0][0], plans[0][1], None)
        cv_lg, th_lg = onnx.logits_many(sval_xn), _torch_logits(torch, net, sval_xn)
        diff = float(np.abs(cv_lg - th_lg).max()) if len(sval_xn) else 0.0
        same = all(read_answers(a)[0][0] == read_answers(b)[0][0] for a, b in zip(cv_lg, th_lg, strict=True))
        # 온도와 기준: 모은 읽기 전체에서
        temp, tinfo = calib.fit_temperature(reads, lambda r, t: truth_prob(r.scores, r.key, r.candidates, r.truth, t))
        choices = [choose(r.scores, r.key, r.candidates, temp) for r in reads]
        preds = [(c.value, c.confidence, c.answer) for c in choices]
        truths = [r.truth for r in reads]
        table = calib.threshold_table(preds, truths)
        chosen = calib.choose_threshold(table, args.target_auto_error, args.min_val_auto)
        reason = None if chosen else (calib.why_no_threshold(table, args.target_auto_error, args.min_val_auto) if reads
                                      else "기준을 정할 읽기가 없다 (검증 날짜·묶음의 셀이 없다)")
        values = {k: candidates_for(k, final_tr, MetaTrainArgs(name="", keys=args.keys)) for k in args.keys}
        card = {
            "name": args.name, "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "code_version": code_version(), "spec": spec.to_dict(),
            "input": {"width": INPUT_W, "height": INPUT_H, "channels": N_INPUT_CHANNELS, "position": bool(args.position),
                      "preprocess": "recognize/digits/model.py: resize_input → normalize (잉크, 세로 위치, 가로 위치)"
                                    + ("" if args.position else " — 위치 채널은 0")},
            "chars": CHARS, "kinds": [],
            "architecture": {"layers": describe_net(args.channels), "channels": list(args.channels), "params": n_params,
                             "onnx_bytes": (tmp / "model.onnx").stat().st_size, "exporter": exporter, "batch": 1},
            "train_args": {k: v for k, v in asdict(args).items() if k not in ("name", "seed", "workers", "template_values",
                                                                              "keys", "extra_digits")}
            | {"synthetic": len(syn_x), "geometry": {k: list(g) for k, g in geoms.items()},
               "mix": (f"실제:합성 = {args.real_share:g}:{1 - args.real_share:g} (배치 기준)" if final_tr and syn_x
                       else ("합성만" if syn_x else "실제만"))
                      + (f" — 합성 쪽에 숫자 칸(--extra-digits) {len(extra)}개를 섞는다" if extra else "")},
            "seed": args.seed,
            "data": {"crops": {"lines": crops.lines, "skipped": crops.skipped | ({"not_digits": skipped} if skipped else {}),
                               "splits": sorted(crops.splits)},
                     "train": count_summary(final_tr), "all": count_summary(samples),
                     "synthetic": {"cells": len(syn_x), "val_cells": len(sval_x)},
                     **({"extra_digits": extra_info} if extra_info else {}),
                     "val_rule": {"salt": data.val_salt(split_salt), "share": args.val_share,
                                  "rule": "sha256(salt:날짜) 앞 32비트 / 2^32 < share 이면 검증 (evaluate/split.py)"}},
            "validation": {"source": method["method"], "reads": len(reads), "score": calib.score(preds, truths,
                                                                                               [r.candidates for r in reads]),
                           "best_step": best["step"], "train_seconds": round(train_seconds, 1), "total_seconds": None,
                           "export_check": {"max_abs_diff": diff, "same_answers": bool(same), "cells": len(sval_xn)},
                           "calibration": tinfo},
            "temperature": temp,
            "auto_accept": {"target": args.target_auto_error, "threshold": None if chosen is None else chosen["threshold"],
                            "met": chosen is not None, "reason": reason, "min_auto": args.min_val_auto,
                            "auto": None if chosen is None else chosen["auto"],
                            "errors": None if chosen is None else chosen["errors"],
                            "upper95": None if chosen is None else chosen["error_ci95"][1],
                            "basis": f"읽기 {len(reads)}번 ({method['method']})",
                            "rule": "오류율(자동 적재된 쪽 중 정답과 다른 비율)이 목표 이하인 가장 낮은 임계값, 자동 적재된 읽기가 "
                                    "min_auto 이상 (ADR 0012, tasks/0004 4.5)", "table": table},
            "libraries": libraries(torch), "model_sha256": sha256_file(tmp / "model.onnx"),
            "meta": {"reader": "digits", "keys": list(args.keys),
                     "classes": {k: {"n": len(v)} for k, v in values.items()},
                     "template_values": {k: len(v) for k, v in args.template_values.items() if k in args.keys},
                     "cv": method, "unlisted_ratio": UNLISTED_RATIO,
                     "candidates": "classes.json 의 값 + 템플릿의 header_<키> (날짜의 부분은 월 1–12, 일 1–31)"},
        }
        card["validation"]["total_seconds"] = round(time.time() - t_start, 1)
        (tmp / "card.json").write_text(json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8")
        (tmp / "classes.json").write_text(json.dumps({"reader": "digits", "keys": list(args.keys), "values": values},
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
    progress(f"모델: {out_dir} · 읽기 {len(reads)}번 ({method['method']}) · 온도 {temp:g} · 자동 적재 기준 "
             + (f"{aa['threshold']} (자동 적재 {aa['auto']} 중 오류 {aa['errors']}, 95 % 상한 {aa['upper95']:.1%})"
                if aa["met"] else f"없음 ({aa['reason']})") + f" · {time.time() - t_start:.0f}s")
    return card
