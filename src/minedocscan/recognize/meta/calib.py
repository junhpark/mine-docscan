"""메타 필드 읽기의 보정: 온도 하나, 임계값별 자동 적재율·오류율(윌슨 구간), 기준 고르기 — torch 없이 (tasks/0004 4.5).

0003 의 규칙(ADR 0012)을 그대로 쓴다: 오류율이 목표 이하인 가장 낮은 임계값, 그 임계값에서 자동 적재된 칸이 최소 수 미만이면
기준 없음, 고른 기준 옆에는 윌슨 95 % 상한. 다른 것은 "답"의 모양뿐이다 — 숫자 칸의 답은 숫자열·빈 칸·거절이고, 메타 필드의
답은 고른 후보(value)·목록에 없는 값(unlisted)·거절(reject)이다. 자동 적재될 수 있는 것은 value 뿐이다.

읽기(read)는 (점수, 키, 후보 목록, 정답) 묶음이다. --cv 면 묶음마다 다른 모델·다른 후보 목록으로 읽은 것이 섞여 있다.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ...evaluate.stats import wilson
from ..digits.calib import COARSE_GRID, THRESHOLD_GRID, choose_threshold, why_no_threshold  # noqa: F401

CV_READS = "cv-reads.jsonl"


@dataclass
class Read:
    """검증에서 한 번 읽은 것. scores: 숫자 모델은 (C, T) 점수, 분류기는 종류별 점수. candidates: 그 읽기의 후보 목록."""

    scores: np.ndarray
    key: str
    candidates: list[str]
    truth: str
    date: str | None = None
    fold: int | None = None
    extra: dict = field(default_factory=dict)


def fit_temperature(reads: list[Read], prob: Callable[[Read, float], float | None]) -> tuple[float, dict]:
    """정답 후보의 평균 음의 로그 확률이 가장 작은 온도. prob(read, T) = 정답의 정규화 확률 (정답이 후보에 없으면 None).
    0.5–4.0 을 0.25 간격으로 찾고 그 둘레를 0.05 간격으로 (digits/calib.fit_temperature 와 같은 격자)."""
    usable = [r for r in reads if r.truth in r.candidates]
    if not usable:
        return 1.0, {"reads": 0}
    cache: dict[float, float] = {}

    def nll(t: float) -> float:
        t = round(t, 2)
        if t not in cache:
            ps = [prob(r, t) for r in usable]
            cache[t] = float(np.mean([min(-math.log(max(p, 1e-12)), 30.0) for p in ps if p is not None]))
        return cache[t]

    coarse = min(COARSE_GRID, key=lambda t: (nll(t), abs(t - 1.0)))
    fine = [round(coarse + 0.05 * k, 2) for k in range(-4, 5) if 0.3 <= coarse + 0.05 * k <= 4.25]
    best = min(fine, key=lambda t: (nll(t), abs(t - 1.0)))
    return float(best), {"reads": len(usable), "nll_at_1": round(nll(1.0), 5), "nll": round(nll(best), 5)}


def threshold_table(preds: list[tuple[str, float, str]], truths: list[str], grid=THRESHOLD_GRID) -> list[dict]:
    """preds: (값, 신뢰도, 답의 종류 value|unlisted|reject). 자동 적재 = value 이고 신뢰도 ≥ 임계값. 틀림 = 값 ≠ 정답.
    행의 모양은 digits/calib.threshold_table 과 같다 (choose_threshold·why_no_threshold 를 같이 쓴다)."""
    n = len(preds)
    rows = []
    for t in grid:
        auto = [(v, y) for (v, c, a), y in zip(preds, truths, strict=True) if a == "value" and c >= t]
        k = sum(v != y for v, y in auto)
        lo, hi = wilson(k, len(auto))
        rows.append({"threshold": t, "auto": len(auto), "auto_rate": round(len(auto) / n, 4) if n else 0.0, "errors": k,
                     "error_rate": round(k / len(auto), 4) if auto else None, "error_ci95": [round(lo, 4), round(hi, 4)]})
    return rows


def score(preds: list[tuple[str, float, str]], truths: list[str], candidates: list[list[str]]) -> dict:
    """정확도(고른 값 = 정답), 정답이 후보에 있던 것만의 정확도, 목록에 없는 값으로 답한 수, 정답이 목록 밖인데 그렇게 답한 수."""
    n = len(preds)
    ok = sum(v == y for (v, _c, _a), y in zip(preds, truths, strict=True))
    inl = [(p, y) for p, y, c in zip(preds, truths, candidates, strict=True) if y in c]
    outl = [(p, y) for p, y, c in zip(preds, truths, candidates, strict=True) if y not in c]
    return {"n": n, "correct": ok, "accuracy": round(ok / n, 4) if n else None,
            "listed": {"n": len(inl), "correct": sum(p[0] == y for p, y in inl),
                       "accuracy": round(sum(p[0] == y for p, y in inl) / len(inl), 4) if inl else None},
            "unlisted_answers": sum(p[2] == "unlisted" for p in preds),
            "truth_unlisted": {"n": len(outl), "answered_unlisted": sum(p[2] == "unlisted" for p, _y in outl),
                               "answered_value": sum(p[2] == "value" for p, _y in outl)},
            "rejects": sum(p[2] == "reject" for p in preds)}


def status_of(answer: str, confidence: float, threshold: float) -> str:
    """기계의 상태 (doc_page_meta.machine_status): value 이고 기준 이상 → auto, value → pending, unlisted → unlisted,
    reject → pending."""
    if answer == "value":
        return "auto" if confidence >= threshold else "pending"
    return "unlisted" if answer == "unlisted" else "pending"


def write_cv_reads(path, reads: list[Read], preds: list[tuple[str, float, str]]) -> None:
    """--cv 의 읽기(묶음마다의 모델로 읽은 것)를 모델 폴더에 남긴다 — recognizer eval --split val 이 쓴다 (묶음마다의 모델은 남기지 않으므로).
    값(이름·차량번호)은 적지 않는다: 필드, 키, 날짜, 묶음, 맞았나, 신뢰도(카드의 온도로), 답의 종류, 정답이 그 읽기의 후보에 있었나."""
    with open(path, "w", encoding="utf-8") as fh:
        for r, (v, c, a) in zip(reads, preds, strict=True):
            fh.write(json.dumps({"field_id": r.extra.get("field_id"), "key": r.key, "date": r.date, "fold": r.fold,
                                 "correct": v == r.truth, "confidence": round(float(c), 6), "answer": a,
                                 "truth_listed": r.truth in r.candidates}, ensure_ascii=False) + "\n")


def load_cv_reads(path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def cv_preds(rows: list[dict]) -> tuple[list[tuple[str, float, str]], list[str], list[list[str]]]:
    """write_cv_reads 의 줄 → (preds, truths, candidates). 값 대신 자리표시자: 정답 "y", 맞으면 고른 값도 "y"."""
    preds = [("y" if r["correct"] else "n", r["confidence"], r["answer"]) for r in rows]
    return preds, ["y"] * len(rows), [["y"] if r["truth_listed"] else [] for r in rows]
