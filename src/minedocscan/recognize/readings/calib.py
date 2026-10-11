"""소수·시각 모델의 온도 보정·자동 적재 기준·성적 — torch 없이 (tasks/0011 4.3, 4.4).

숫자 모델(digits/calib.py)의 규칙 그대로 (ADR 0012): 내보낸 ONNX 를 OpenCV 로 읽은 검증 칸(검증 날짜 또는 --cv 의 묶음 읽기)에서
  1) 온도 하나: 정답의 음의 로그우도가 가장 작은 값. 정답은 표기가 여럿이다 — NLL = −log Σ P(표기) (4.4). 표기마다 파이썬으로
     CTC 전방 알고리즘을 돌리면 표기 128개 × 온도 약 24개에 검증 칸 몇백 개에도 오래 걸린다 → (칸, 표기) 쌍을 한 배열로 펼쳐
     시간 축으로만 반복한다 (ctc_nll_many)
  2) 임계값별 자동 적재율과 자동 적재 오류율(분자·분모·윌슨 95 % 구간)
  3) 자동 적재 기준 = 오류율이 목표 이하인 가장 낮은 임계값 (자동 적재된 검증 칸이 min_auto 미만이면 없음)
자동 적재 대상 = 값(점으로 쓴 시각이 아닌 것 — reading 칸)과 빈 칸. 거절은 신뢰도와 상관없이 검수 대기. 기준은 하나 — 형식마다
두지 않는다 (칸이 적다). 맞았는가 = formats.same_value (기계가 적힌 대로 읽은 글자 ↔ 정규화한 정답).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ...evaluate.stats import wilson
from ...forms.formats import dotted_clock, same_value
from ..digits.calib import (  # noqa: F401 — 같은 규칙
    COARSE_GRID,
    THRESHOLD_GRID,
    choose_threshold,
    why_no_threshold,
)
from ..digits.model import BLANK
from .model import FORMATS, REJECT, Answer, decode, encode

NLL_CAP = 50.0                     # 한 칸의 음의 로그우도 상한 (숫자 모델과 같다 — 틀린 정답 하나가 평균을 휘두르지 않게)


# ── 여러 표기의 CTC 우도 ──────────────────────────────────────────────────
def _log_softmax(z: np.ndarray, temperature: float) -> np.ndarray:
    """(N, C, T) 점수 → (N, T, C) 로그 확률."""
    x = np.transpose(z, (0, 2, 1)).astype(np.float64) / float(temperature)
    x = x - x.max(axis=2, keepdims=True)
    return x - np.log(np.exp(x).sum(axis=2, keepdims=True))


def pairs_of(targets: list[list[str]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """칸마다의 표기들 → (칸 번호 (P,), 늘인 목표 (P, S) — blank 를 사이사이에, 늘인 길이 (P,))."""
    owner, exts = [], []
    for i, ts in enumerate(targets):
        for t in ts:
            ext = [BLANK]
            for c in encode(t):
                ext += [c, BLANK]
            owner.append(i)
            exts.append(ext)
    S = max((len(e) for e in exts), default=1)
    E = np.full((len(exts), S), BLANK, np.int64)
    for k, e in enumerate(exts):
        E[k, :len(e)] = e
    return np.array(owner, np.int64), E, np.array([len(e) for e in exts], np.int64)


def ctc_nll_many(logp: np.ndarray, owner: np.ndarray, ext: np.ndarray, lens: np.ndarray, n_cells: int) -> np.ndarray:
    """(N, T, C) 로그 확률, (칸, 표기) 쌍 → 칸마다 −log Σ P(표기) (N,). CTC 전방 알고리즘을 쌍 전부에 한 번에 (시간 축으로만 반복)."""
    P, S = ext.shape
    if P == 0:
        return np.zeros(n_cells)
    T = logp.shape[1]

    def at(t: int) -> np.ndarray:                                        # (P, S): 그 시각의 늘인 목표 자리의 로그 확률
        return np.take_along_axis(logp[:, t, :][owner], ext, axis=1)

    skip = np.zeros((P, S), bool)
    skip[:, 2:] = (ext[:, 2:] != BLANK) & (ext[:, 2:] != ext[:, :-2])
    valid = np.arange(S)[None, :] < lens[:, None]
    ninf = -np.inf
    a = np.full((P, S), ninf)
    first = at(0)
    a[:, 0] = first[:, 0]
    a[:, 1] = np.where(lens > 1, first[:, 1], ninf)
    pad1 = np.full((P, 1), ninf)
    pad2 = np.full((P, 2), ninf)
    with np.errstate(invalid="ignore"):
        for t in range(1, T):
            s1 = np.concatenate([pad1, a[:, :-1]], 1)
            s2 = np.where(skip, np.concatenate([pad2, a[:, :-2]], 1), ninf)
            a = np.logaddexp(np.logaddexp(a, s1), s2) + at(t)
            a = np.where(valid, a, ninf)
        last = np.take_along_axis(a, (lens - 1)[:, None], 1)[:, 0]
        prev = np.where(lens > 1, np.take_along_axis(a, np.maximum(lens - 2, 0)[:, None], 1)[:, 0], ninf)
        tot = np.logaddexp(last, prev)                                   # 표기 하나의 로그 우도
    out = np.full(n_cells, ninf)
    np.logaddexp.at(out, owner, tot)                                      # 칸마다 표기의 합
    return -out


def mean_nll(logits: np.ndarray, pairs, temperature: float) -> float:
    owner, ext, lens = pairs
    nll = ctc_nll_many(_log_softmax(logits, temperature), owner, ext, lens, len(logits))
    return float(np.mean(np.minimum(nll, NLL_CAP))) if len(nll) else 0.0


def fit_temperature(logits: list[np.ndarray], targets: list[list[str]]) -> tuple[float, dict]:
    """정답(표기들)의 평균 음의 로그우도가 가장 작은 온도 (0.5–4.0 을 0.25 간격으로 찾고 그 둘레를 0.05 간격으로 — 숫자 모델과 같다).
    돌려주는 값: (온도, {"cells", "pairs", "nll_at_1", "nll"})."""
    if not logits:
        return 1.0, {"cells": 0}
    z = np.stack(logits)
    pairs = pairs_of(targets)
    cache: dict[float, float] = {}

    def f(t: float) -> float:
        t = round(t, 2)
        if t not in cache:
            cache[t] = mean_nll(z, pairs, t)
        return cache[t]

    coarse = min(COARSE_GRID, key=lambda t: (f(t), abs(t - 1.0)))
    fine = [round(coarse + 0.05 * k, 2) for k in range(-4, 5) if 0.3 <= coarse + 0.05 * k <= 4.25]
    best = min(fine, key=lambda t: (f(t), abs(t - 1.0)))
    return float(best), {"cells": len(logits), "pairs": int(len(pairs[0])), "nll_at_1": round(f(1.0), 5), "nll": round(f(best), 5)}


# ── 읽기와 판정 ────────────────────────────────────────────────────────────
@dataclass
class Read:
    fmt: str
    truth: str                      # 정규화한 값 | "" | "?"
    answers: list[Answer]

    @property
    def top(self) -> Answer:
        return self.answers[0]


def read_all(logits: list[np.ndarray], fmts: list[str], truths: list[str], temperature: float) -> list[Read]:
    return [Read(f, y, decode(z, f, temperature)) for z, f, y in zip(logits, fmts, truths, strict=True)]


def is_dotted(fmt: str, a: Answer) -> bool:
    """reading 칸에 점으로 쓴 시각으로 읽은 답 — 늘 사람이 정한다 (format_row 와 같은 조건)."""
    return fmt == "reading" and a.kind == "value" and dotted_clock(a.text) is not None


def eligible(fmt: str, a: Answer) -> bool:
    """신뢰도가 높으면 자동 적재될 수 있는 답인가: 값(점으로 쓴 시각이 아닌 것) 또는 빈 칸."""
    return a.kind == "empty" or (a.kind == "value" and not is_dotted(fmt, a))


def correct(fmt: str, a: Answer, truth: str) -> bool:
    """맞았는가: 빈 칸 ↔ "", 거절 ↔ "?", 값 ↔ same_value(형식, 적힌 글자, 정답)."""
    if truth == REJECT:
        return a.kind == "reject"
    if truth == "":
        return a.kind == "empty"
    return a.kind == "value" and same_value(fmt, a.text, truth)


def flags(reads: list[Read]) -> list[dict]:
    """읽기 → 값 없는 판정 줄: 형식, 정답의 종류(value·empty·illegible), 답의 종류·신뢰도, 맞았는가, 자동 적재 대상인가, 점으로 쓴
    시각인가. 성적·임계값 표는 이 줄로 센다 — --cv 의 읽기 기록(cv-reads.jsonl)도 같은 꼴이다 (값을 남기지 않는다)."""
    return [{"fmt": r.fmt, "truth": "illegible" if r.truth == REJECT else ("empty" if r.truth == "" else "value"),
             "answer": r.top.kind, "confidence": r.top.prob, "correct": correct(r.fmt, r.top, r.truth),
             "eligible": eligible(r.fmt, r.top), "dotted": is_dotted(r.fmt, r.top)} for r in reads]


def threshold_table(reads: list[Read] | list[dict], grid=THRESHOLD_GRID) -> list[dict]:
    """임계값마다: 자동 적재 수(값/빈 칸), 자동 적재율, 틀린 수, 오류율과 윌슨 구간 (digits/calib.threshold_table 과 같은 꼴)."""
    rows = _rows(reads)
    n = len(rows)
    out = []
    for t in grid:
        auto = [r for r in rows if r["confidence"] >= t and r["eligible"]]
        k = sum(not r["correct"] for r in auto)
        lo, hi = wilson(k, len(auto))
        out.append({"threshold": t, "auto": len(auto), "auto_value": sum(r["answer"] == "value" for r in auto),
                    "auto_empty": sum(r["answer"] == "empty" for r in auto), "auto_rate": round(len(auto) / n, 4) if n else 0.0,
                    "errors": k, "error_rate": round(k / len(auto), 4) if auto else None,
                    "error_ci95": [round(lo, 4), round(hi, 4)]})
    return out


def _rows(reads) -> list[dict]:
    return flags(reads) if reads and isinstance(reads[0], Read) else list(reads)


def _acc(items: list[bool]) -> dict:
    return {"n": len(items), "correct": int(sum(items)),
            "accuracy": round(sum(items) / len(items), 4) if items else None}


def score(reads: list[Read] | list[dict], threshold: float | None = None) -> dict:
    """형식마다: 값 정확도(same_value)·빈 칸·거절, 점으로 쓴 시각으로 읽은 칸의 수 — 그리고 전체.
    threshold 를 주면 그 기준에서의 자동 적재(분자·분모·윌슨 구간)도."""
    rows = _rows(reads)

    def one(rs: list[dict]) -> dict:
        out = {"n": len(rs), "all": _acc([r["correct"] for r in rs]),
               "value": _acc([r["correct"] for r in rs if r["truth"] == "value"]),
               "empty": _acc([r["correct"] for r in rs if r["truth"] == "empty"]),
               "illegible": _acc([r["correct"] for r in rs if r["truth"] == "illegible"]),
               "dotted": sum(bool(r["dotted"]) for r in rs)}
        if threshold is not None and math.isfinite(threshold):
            auto = [r for r in rs if r["confidence"] >= threshold and r["eligible"]]
            k = sum(not r["correct"] for r in auto)
            lo, hi = wilson(k, len(auto))
            out["auto"] = {"n": len(auto), "rate": round(len(auto) / len(rs), 4) if rs else None, "errors": k,
                           "error_rate": round(k / len(auto), 4) if auto else None, "error_ci95": [round(lo, 4), round(hi, 4)]}
        return out

    out = one(rows)
    out["by_format"] = {f: one([r for r in rows if r["fmt"] == f]) for f in FORMATS if any(r["fmt"] == f for r in rows)}
    return out
