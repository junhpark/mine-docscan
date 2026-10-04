"""닫힌 목록에서 고르기 — 숫자 모델(CTC)이 읽은 것을 후보 목록에 맞춘다 (tasks/0004 4.1). torch 없이.

자유롭게 읽은 문자열을 그대로 쓰지 않는다. 후보마다 "이 그림이 그 문자열일 가능도"(CTC 전방 알고리즘, digits/calib.ctc_nll)를
계산해 가장 그럴듯한 후보를 고른다. 신뢰도 = 후보들 사이에서 정규화한 확률. 그래서 한 글자가 애매해도(4135 / 4185) 목록에
있는 번호 중에서 고르고, 그 애매함이 신뢰도에 그대로 드러난다.

자유롭게 읽은 답(빔 탐색의 첫째)이 목록에 없고 어느 후보보다도 **뚜렷이** 그럴듯하면(UNLISTED_RATIO 배) "목록에 없는 값"
(unlisted — 새 차)이다. 그 답이 숫자가 아니면(빈 칸, "?") 거절(reject). 둘 다 자동 적재되지 않는다.

날짜의 월·일은 0 을 붙여 쓰기도 한다 ("07"). 후보 "7" 의 가능도 = "7" 과 "07" 의 가능도의 합. 차량번호는 쓴 그대로 비교한다
(앞의 0 도 번호의 일부다).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ...forms.template import DATE_PARTS
from ..digits.calib import ctc_nll
from ..digits.model import REJECT, beam_search, encode, log_softmax

# 자유롭게 읽은 답이 가장 그럴듯한 후보보다 이 배수 이상 그럴듯해야 "목록에 없는 값"이라고 한다 (합성 데이터에서 정했다:
# 목록 안의 번호를 쓴 쪽에서는 자유 답이 후보와 같거나 후보보다 덜 그럴듯했고, 목록 밖의 번호를 쓴 쪽에서는 최고 후보보다
# 수십–수천 배 그럴듯했다. 실데이터의 메타 필드로 다시 본다 — 8절)
UNLISTED_RATIO = 10.0

ANSWERS = ("value", "unlisted", "reject")


@dataclass
class Choice:
    value: str                       # 고른 후보 (value), 자유롭게 읽은 값 (unlisted), "" 또는 "?" (reject)
    confidence: float                # value: 후보들 사이에서 정규화한 확률. unlisted·reject: 자유 답의 확률
    answer: str                      # value | unlisted | reject
    candidates: list[str] = field(default_factory=list)      # 그럴듯한 순서의 후보 (최대 5)
    free: str = ""                   # 자유롭게 읽은 답 (빔 탐색 첫째)


def forms_of(key: str, value: str) -> list[str]:
    """종이에 쓰는 꼴들. 날짜의 부분은 0 을 붙인 꼴도 ("7" → "7", "07")."""
    if key in DATE_PARTS and value.isdigit() and len(value) == 1:
        return [value, "0" + value]
    return [value]


def canonical(key: str, text: str) -> str:
    """자유롭게 읽은 문자열 → 후보와 비교하는 표기. 날짜의 부분은 앞의 0 을 뗀다."""
    if key in DATE_PARTS and text.isdigit():
        return str(int(text))
    return text


def readable(value: str) -> bool:
    """숫자 모델이 읽을 수 있는 후보 (숫자만). 'V-101' 같은 값은 숫자 모델의 후보가 될 수 없다."""
    return value.isdigit()


def _lse(xs: list[float]) -> float:
    m = max(xs)
    if m == -math.inf:
        return m
    return m + math.log(sum(math.exp(x - m) for x in xs))


def candidate_logliks(logp: np.ndarray, key: str, candidates: list[str]) -> dict[str, float]:
    """후보 → 로그 가능도 (꼴들의 합)."""
    return {c: _lse([-ctc_nll(logp, encode(f)) for f in forms_of(key, c)]) for c in candidates if readable(c)}


def choose(logits: np.ndarray, key: str, candidates: list[str], temperature: float = 1.0,
           ratio: float = UNLISTED_RATIO) -> Choice:
    """(C, T) 점수와 후보 목록 → Choice."""
    logp = log_softmax(logits, temperature)
    beams = beam_search(logp)
    free = beams[0][0] if beams else ""
    free_ll = -ctc_nll(logp, encode(free)) if free else (beams[0][1] if beams else -math.inf)
    ll = candidate_logliks(logp, key, candidates)
    ranked = sorted(ll, key=lambda c: (-ll[c], c))
    free_c = canonical(key, free) if REJECT not in free else free
    if ranked:
        best = ranked[0]
        total = _lse(list(ll.values()))
        conf = math.exp(ll[best] - total) if total > -math.inf else 0.0
        if free_c in ll or free_ll < ll[best] + math.log(ratio):
            return Choice(best, conf, "value", ranked[:5], free)
    if free and free.isdigit():
        return Choice(free_c, math.exp(free_ll), "unlisted", ranked[:5], free)
    return Choice(REJECT if REJECT in free else "", math.exp(free_ll) if free_ll > -math.inf else 0.0, "reject",
                  ranked[:5], free)


def truth_prob(logits: np.ndarray, key: str, candidates: list[str], truth: str, temperature: float) -> float | None:
    """정답이 후보에 있을 때 그 정규화 확률 (온도 맞추기). 없으면 None."""
    if truth not in candidates or not readable(truth):
        return None
    ll = candidate_logliks(log_softmax(logits, temperature), key, candidates)
    total = _lse(list(ll.values()))
    return math.exp(ll[truth] - total) if total > -math.inf else None
