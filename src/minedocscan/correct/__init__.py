"""교정(correct) 단계의 인터페이스와 등록소.

원칙: 교정은 생성이 아니라 선택이다. 교정기는 후보(같은 장비·같은 열에서 반복된 문구 등)를 내고
그중에서 고르거나 편집거리 제한 안에서만 고친다. 제한을 넘으면 원문을 유지하고 검수로 보낸다.
등록번호·차량번호 같은 고유값은 교정 대상이 아니다 — 마스터와 매칭한다.

지금은 통과(PassThrough)만 있다. 계획된 구현:
  - phrase : 행(장비)·열별 문구 DB 에서 top-k 후보를 내고 편집거리로 스냅
  - llm    : 후보 목록을 프롬프트에 넣어 선택만 하게 하는 도메인 LLM
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..recognize.base import CellContext, Recognition


class Corrector(Protocol):
    name: str

    def correct(self, recs: list[Recognition], contexts: list[CellContext]) -> list[Recognition]: ...


class PassThrough:
    name = "none"

    def correct(self, recs, contexts):
        return recs


REGISTRY: dict[str, Callable[..., Corrector]] = {"none": PassThrough}


def register(name: str, factory: Callable[..., Corrector]) -> None:
    REGISTRY[name] = factory


def get_corrector(name: str, **kwargs) -> Corrector:
    if name not in REGISTRY:
        raise KeyError(f"등록되지 않은 교정 백엔드: {name} (사용 가능: {sorted(REGISTRY)})")
    return REGISTRY[name](**kwargs)
