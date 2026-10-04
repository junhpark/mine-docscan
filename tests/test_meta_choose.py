"""닫힌 목록에서 고르기 (tasks/0004 4.1) — torch 없이, 손으로 만든 CTC 점수로."""
import math

import numpy as np

from minedocscan.recognize.digits.model import CHARS, N_CLASSES
from minedocscan.recognize.meta import calib
from minedocscan.recognize.meta.choose import UNLISTED_RATIO, canonical, choose, forms_of, truth_prob


def logits_for(path: list[str], sure: float = 6.0, alt: dict[int, tuple[str, float]] | None = None) -> np.ndarray:
    """칸마다 한 문자(빈칸은 "-")를 크게. alt: 칸 → (다른 문자, 그 점수) — 애매한 글자."""
    z = np.zeros((N_CLASSES, len(path)), np.float32)
    for t, ch in enumerate(path):
        z[0 if ch == "-" else CHARS.index(ch) + 1, t] = sure
        if alt and t in alt:
            c, s = alt[t]
            z[0 if c == "-" else CHARS.index(c) + 1, t] = s
    return z


def path_of(text: str) -> list[str]:
    out = ["-"]
    for ch in text:
        out += [ch, "-"]
    return out + ["-"] * (24 - len(out))


def test_picks_the_listed_value_and_normalizes_over_candidates():
    z = logits_for(path_of("4135"), alt={5: ("8", 5.5)})                 # 셋째 글자가 3 인지 8 인지 애매하다
    c = choose(z, "vehicle_no", ["4127", "4135", "4185", "5260"])
    assert c.answer == "value" and c.value == "4135" and c.candidates[:2] == ["4135", "4185"]
    assert 0.5 < c.confidence < 0.75                                         # 두 후보로 나뉜다
    c2 = choose(z, "vehicle_no", ["4127", "4135", "5260"])                  # 4185 가 목록에 없으면 확신한다
    assert c2.value == "4135" and c2.confidence > 0.99


def test_unlisted_and_reject():
    z = logits_for(path_of("7391"))
    c = choose(z, "vehicle_no", ["4127", "4135", "5260"])
    assert c.answer == "unlisted" and c.value == "7391" and calib.status_of(c.answer, c.confidence, 0.5) == "unlisted"
    blank = logits_for(["-"] * 24)
    r = choose(blank, "vehicle_no", ["4127"])
    assert r.answer == "reject" and r.value == "" and calib.status_of(r.answer, r.confidence, 0.0) == "pending"
    q = choose(logits_for(path_of("?")), "vehicle_no", ["4127"])
    assert q.answer == "reject" and q.value == "?"
    # 목록이 비어 있으면 읽은 값은 언제나 목록 밖
    assert choose(logits_for(path_of("4127")), "vehicle_no", []).answer == "unlisted"
    assert UNLISTED_RATIO > 1


def test_date_parts_accept_a_leading_zero():
    assert forms_of("date.day", "7") == ["7", "07"] and forms_of("vehicle_no", "0412") == ["0412"]
    assert canonical("date.day", "07") == "7" and canonical("vehicle_no", "0412") == "0412"
    z = logits_for(path_of("07"))
    days = [str(d) for d in range(1, 32)]
    c = choose(z, "date.day", days)
    assert c.answer == "value" and c.value == "7" and c.confidence > 0.9
    # 차량번호는 앞의 0 도 번호다: 0412 와 412 는 다른 후보
    v = choose(logits_for(path_of("0412")), "vehicle_no", ["412", "0412"])
    assert v.value == "0412"


def test_temperature_and_threshold_table():
    reads = [calib.Read(logits_for(path_of(t), sure=s), "vehicle_no", ["4127", "4135"], t)
             for t, s in (("4127", 3.0), ("4135", 3.0), ("4127", 2.0))]
    temp, info = calib.fit_temperature(reads, lambda r, t: truth_prob(r.scores, r.key, r.candidates, r.truth, t))
    assert 0.3 <= temp <= 4.25 and info["reads"] == 3
    preds = [("4127", 0.99, "value"), ("4135", 0.6, "value"), ("7391", 0.99, "unlisted"), ("4127", 0.95, "value")]
    truths = ["4127", "4135", "7391", "4135"]
    rows = {r["threshold"]: r for r in calib.threshold_table(preds, truths)}
    assert (rows[0.5]["auto"], rows[0.5]["errors"]) == (3, 1) and (rows[0.97]["auto"], rows[0.97]["errors"]) == (1, 0)
    s = calib.score(preds, truths, [["4127", "4135"]] * 4)
    assert s["truth_unlisted"] == {"n": 1, "answered_unlisted": 1, "answered_value": 0} and s["accuracy"] == 0.75
    assert math.isclose(sum(1 for _ in rows), 14)
