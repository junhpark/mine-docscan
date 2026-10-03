"""온도 보정과 자동 적재 기준 — torch 없이 (tasks/0003 4.3, 4.4, 4.6).

학습이 끝나면 **내보낸 ONNX 를 OpenCV 로 읽어** 검증 날짜의 셀에서:
  1) 온도 하나: 정답 문자열의 음의 로그우도(CTC)가 가장 작은 값을 격자에서 고른다.
  2) 임계값별 자동 적재율과 자동 적재 오류율(분자·분모·윌슨 95 % 구간).
  3) 자동 적재 기준 = 오류율이 목표 이하인 가장 낮은 임계값. 없으면 "자동 적재 없음".
자동 적재 대상은 4.4 의 표대로다: 숫자열(범위 안)과 빈 칸. 거절("?")·범위 밖은 신뢰도와 상관없이 검수 대기.
"""
from __future__ import annotations

import math

import numpy as np

from .model import BLANK, REJECT, answer_kind, encode, log_softmax, read_answers

COARSE_GRID = tuple(round(0.5 + 0.25 * i, 2) for i in range(15))               # 0.5 – 4.0
THRESHOLD_GRID = (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.98, 0.99, 0.995, 0.998, 0.999)


def ctc_nll(logp: np.ndarray, target: list[int]) -> float:
    """(T, C) 로그 확률에서 목표 열의 음의 로그우도 (CTC 전방 알고리즘)."""
    ext = [BLANK]
    for c in target:
        ext += [c, BLANK]
    ext_a = np.array(ext)
    S = len(ext)
    skip = np.zeros(S, bool)
    skip[2:] = (ext_a[2:] != BLANK) & (ext_a[2:] != ext_a[:-2])          # 같은 글자 사이의 blank 는 건너뛸 수 없다
    lp = logp[:, ext_a]
    a = np.full(S, -np.inf)
    a[0] = lp[0, 0]
    if S > 1:
        a[1] = lp[0, 1]
    ninf = np.array([-np.inf, -np.inf])
    for t in range(1, lp.shape[0]):
        s1 = np.concatenate([ninf[:1], a[:-1]])
        s2 = np.where(skip, np.concatenate([ninf, a[:-2]]), -np.inf)
        a = np.logaddexp(np.logaddexp(a, s1), s2) + lp[t]
    tot = a[S - 1] if S == 1 else np.logaddexp(a[S - 1], a[S - 2])
    return float(-tot)


def fit_temperature(logits: list[np.ndarray], texts: list[str]) -> tuple[float, dict]:
    """정답의 평균 음의 로그우도가 가장 작은 온도 (0.5–4.0, 0.25 간격으로 찾고 그 둘레를 0.05 간격으로).
    돌려주는 값: (온도, {"cells", "nll_at_1", "nll"})."""
    if not logits:
        return 1.0, {"cells": 0}
    targets = [encode(t) for t in texts]
    cache: dict[float, float] = {}

    def mean_nll(temp: float) -> float:
        temp = round(temp, 2)
        if temp not in cache:
            cache[temp] = float(np.mean([min(ctc_nll(log_softmax(z, temp), y), 50.0)
                                         for z, y in zip(logits, targets, strict=True)]))
        return cache[temp]

    coarse = min(COARSE_GRID, key=lambda t: (mean_nll(t), abs(t - 1.0)))
    fine = [round(coarse + 0.05 * k, 2) for k in range(-4, 5) if 0.3 <= coarse + 0.05 * k <= 4.25]
    best = min(fine, key=lambda t: (mean_nll(t), abs(t - 1.0)))
    return float(best), {"cells": len(logits), "nll_at_1": round(mean_nll(1.0), 5), "nll": round(mean_nll(best), 5)}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """이항 비율 k/n 의 윌슨 구간 (기본 95 %). n = 0 이면 (0, 1)."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def in_range(text: str, trips_max: int | None) -> bool:
    return trips_max is None or (text.isdigit() and int(text) <= int(trips_max))


def eligible(answer: str, trips_max: int | None = None) -> bool:
    """신뢰도가 높으면 자동 적재될 수 있는 답인가 (4.4): 숫자열(범위 안) 또는 빈 칸."""
    k = answer_kind(answer)
    return k == "empty" or (k == "value" and in_range(answer, trips_max))


def predict(logits: list[np.ndarray], temperature: float) -> list[tuple[str, float, list[str]]]:
    """[(답, 신뢰도, 후보)]"""
    out = []
    for z in logits:
        ans = read_answers(z, temperature)
        out.append((ans[0][0], float(ans[0][1]), [a for a, _p in ans]))
    return out


def threshold_table(preds: list[tuple[str, float]], truths: list[str], trips_max: int | None = None,
                    grid=THRESHOLD_GRID) -> list[dict]:
    """임계값마다: 자동 적재 수(값/빈 칸), 자동 적재율, 틀린 수, 오류율과 윌슨 구간."""
    n = len(preds)
    rows = []
    for t in grid:
        auto = [(a, y) for (a, c), y in zip(preds, truths, strict=True) if c >= t and eligible(a, trips_max)]
        k = sum(a != y for a, y in auto)
        lo, hi = wilson(k, len(auto))
        rows.append({"threshold": t, "auto": len(auto), "auto_value": sum(a != "" for a, _ in auto),
                     "auto_empty": sum(a == "" for a, _ in auto), "auto_rate": round(len(auto) / n, 4) if n else 0.0,
                     "errors": k, "error_rate": round(k / len(auto), 4) if auto else None,
                     "error_ci95": [round(lo, 4), round(hi, 4)]})
    return rows


def choose_threshold(table: list[dict], target: float) -> dict | None:
    """오류율이 목표 이하인 가장 낮은 임계값의 행. 자동 적재가 하나도 없는 임계값은 고르지 않는다. 없으면 None."""
    for r in sorted(table, key=lambda r: r["threshold"]):
        if r["auto"] > 0 and r["errors"] / r["auto"] <= target:
            return r
    return None


def score(preds: list[tuple[str, float]], truths: list[str]) -> dict:
    """정확도: 전체, 값 있는 칸, 빈 칸, 거절(정답이 "?" 인 칸 — 거절로 답했는가)."""
    def acc(pairs):
        pairs = list(pairs)
        return {"n": len(pairs), "correct": sum(a == y for a, y in pairs),
                "accuracy": round(sum(a == y for a, y in pairs) / len(pairs), 4) if pairs else None}

    pairs = [(a, y) for (a, _c), y in zip(preds, truths, strict=True)]
    return {"all": acc(pairs), "value": acc((a, y) for a, y in pairs if y not in ("", REJECT)),
            "empty": acc((a, y) for a, y in pairs if y == ""), "illegible": acc((a, y) for a, y in pairs if y == REJECT)}
