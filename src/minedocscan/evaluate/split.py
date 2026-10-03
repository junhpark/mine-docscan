"""평가셋 분할: 날짜로 나누고, 한 번 정하면 바꾸지 않는다 (tasks/0002 4.6, ADR 0009).

같은 날의 일보와 행렬에는 같은 값이 적힌다. 셀 단위로 나누면 학습 쪽에서 본 값이 평가 쪽에 그대로 나오므로 **날짜 단위**로 나눈다.
어느 날짜가 test 인지는 날짜와 사이트 팩의 소금값만으로 정해진다 — 검수가 늘어도, 다른 컴퓨터에서도 같다.
소금값을 바꾸는 것은 평가셋을 버리는 것이다. 바꿨다면 그 전의 수치와 비교하지 않는다.
"""
from __future__ import annotations

import hashlib

SPLITS = ("test", "train")


def split_of(work_date: str | None, salt: str, test_share: float = 0.2) -> str:
    """"test" | "train" | "unknown"(날짜 없음). hash(salt, 날짜)의 앞 32비트를 [0,1) 로 보고 test_share 미만이면 test."""
    if not work_date:
        return "unknown"
    h = hashlib.sha256(f"{salt}:{work_date}".encode()).digest()
    u = int.from_bytes(h[:4], "big") / 2**32
    return "test" if u < test_share else "train"
