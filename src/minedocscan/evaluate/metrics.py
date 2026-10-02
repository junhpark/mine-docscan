"""평가 지표. 모든 변경은 이 수치로 판단한다 (CLAUDE.md).

  CER            문자 오류율 — 수기 텍스트 인식·교정의 품질
  field accuracy 필드 단위 완전 일치율 — 숫자·코드처럼 한 글자만 틀려도 틀린 값
  auto rate      검수 없이 적재된 행의 비율 — 현장 업무 부담을 직접 나타낸다
"""
from __future__ import annotations

from rapidfuzz.distance import Levenshtein


def normalize(s: str | None) -> str:
    """비교 전 정규화: 앞뒤·연속 공백 정리."""
    return " ".join((s or "").split())


def cer(pred: str | None, truth: str | None) -> float:
    """CER = (치환+삭제+삽입)/정답 글자 수. 정답이 비어 있으면 예측도 비어야 0, 아니면 1."""
    p, t = normalize(pred), normalize(truth)
    if not t:
        return 0.0 if not p else 1.0
    return Levenshtein.distance(p, t) / len(t)


def corpus_cer(pairs: list[tuple[str | None, str | None]]) -> float:
    """말뭉치 CER: 전체 편집거리 합 / 전체 정답 글자 수."""
    dist = n = 0
    for pred, truth in pairs:
        p, t = normalize(pred), normalize(truth)
        dist += Levenshtein.distance(p, t)
        n += len(t)
    return dist / n if n else 0.0


def field_accuracy(pairs: list[tuple[str | None, str | None]]) -> float:
    if not pairs:
        return 0.0
    return sum(normalize(p) == normalize(t) for p, t in pairs) / len(pairs)


def auto_rate(statuses: list[str]) -> float:
    if not statuses:
        return 0.0
    return sum(s == "auto" for s in statuses) / len(statuses)
