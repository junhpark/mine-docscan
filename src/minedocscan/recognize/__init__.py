"""인식 백엔드 등록소. 새 백엔드는 register() 로 등록하고 설정의 [recognize] backend 로 고른다."""
from __future__ import annotations

from collections.abc import Callable

from .base import CellContext, Recognition, Recognizer
from .builtin import NullRecognizer, OracleRecognizer, load_answers_json

REGISTRY: dict[str, Callable[..., Recognizer]] = {
    "null": NullRecognizer,
    "oracle": OracleRecognizer,
}


def register(name: str, factory: Callable[..., Recognizer]) -> None:
    REGISTRY[name] = factory


def get_recognizer(name: str, **kwargs) -> Recognizer:
    if name not in REGISTRY:
        raise KeyError(f"등록되지 않은 인식 백엔드: {name} (사용 가능: {sorted(REGISTRY)})")
    return REGISTRY[name](**kwargs)


__all__ = ["CellContext", "Recognition", "Recognizer", "NullRecognizer", "OracleRecognizer",
           "REGISTRY", "register", "get_recognizer", "load_answers_json"]
