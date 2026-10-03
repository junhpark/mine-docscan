"""인식 백엔드 등록소. 새 백엔드는 register() 로 등록하고 설정으로 고른다.

  [recognize] backend = "null"                  기본 백엔드
  [recognize.by_kind] handwritten_number = "…"  칸 종류(handwritten_number, handwritten_text)마다 다른 백엔드

파이프라인은 여전히 Recognizer 하나만 안다 — 종류를 보고 나눠 주는 것도 Recognizer(ByKindRecognizer)다.
백엔드의 팩토리는 settings·site 를 키워드로 받을 수 있다 (받지 않는 팩토리에는 넘기지 않는다).
"""
from __future__ import annotations

import inspect
from collections.abc import Callable

from .base import CellContext, Recognition, Recognizer, spec_for
from .builtin import NullRecognizer, OracleRecognizer, load_answers_json

REGISTRY: dict[str, Callable[..., Recognizer]] = {
    "null": NullRecognizer,
    "oracle": OracleRecognizer,
}

KINDS = ("handwritten_number", "handwritten_text")


def register(name: str, factory: Callable[..., Recognizer]) -> None:
    REGISTRY[name] = factory


def _ensure_plugins() -> None:
    """저장소 안의 선택 백엔드를 등록한다 (무거운 의존성은 각 모듈 안에서만 import 한다)."""
    from .digits import make as make_digits

    REGISTRY.setdefault("digits", make_digits)


def available() -> list[str]:
    """등록된 백엔드 이름 (저장소 안의 선택 백엔드 포함)."""
    _ensure_plugins()
    return sorted(REGISTRY)


def get_recognizer(name: str, **kwargs) -> Recognizer:
    _ensure_plugins()
    if name not in REGISTRY:
        raise KeyError(f"등록되지 않은 인식 백엔드: {name} (사용 가능: {sorted(REGISTRY)})")
    factory = REGISTRY[name]
    try:
        params = inspect.signature(factory).parameters
        accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs = {k: v for k, v in kwargs.items() if accepts_any or k in params}
    except (TypeError, ValueError):
        pass
    return factory(**kwargs)


class ByKindRecognizer:
    """칸 종류마다 다른 백엔드로 나눠 준다. 각 백엔드의 Recognition.backend 가 그대로 doc_field.backend 에 남는다."""

    name = "by_kind"

    def __init__(self, default: Recognizer, by_kind: dict[str, Recognizer]):
        self.default = default
        self.by_kind = dict(by_kind)

    def backend_for(self, kind: str) -> Recognizer:
        return self.by_kind.get(kind, self.default)

    def crop_spec_for(self, kind: str):
        return spec_for(self.backend_for(kind), kind)

    def recognize(self, crops, contexts: list[CellContext]) -> list[Recognition]:
        out: list[Recognition | None] = [None] * len(contexts)
        groups: dict[int, list[int]] = {}
        backends: dict[int, Recognizer] = {}
        for i, c in enumerate(contexts):
            b = self.backend_for(c.kind)
            groups.setdefault(id(b), []).append(i)
            backends[id(b)] = b
        for key, idx in groups.items():
            recs = backends[key].recognize([crops[i] for i in idx], [contexts[i] for i in idx])
            for i, r in zip(idx, recs, strict=True):
                out[i] = r
        return out                                                # type: ignore[return-value]

    def describe(self) -> dict:
        return {"default": self.default.name, "by_kind": {k: b.name for k, b in self.by_kind.items()}}


def build_recognizer(settings, site=None) -> Recognizer:
    """설정에서 인식기를 만든다: [recognize] backend 가 기본, [recognize.by_kind] 가 종류별.
    by_kind 가 없으면 기본 백엔드 하나를 그대로 돌려준다 (지금까지와 같다)."""
    by_kind_names = dict(getattr(settings, "recognizer_by_kind", {}) or {})
    unknown = sorted(set(by_kind_names) - set(KINDS))
    if unknown:
        raise ValueError(f"[recognize.by_kind] 의 종류는 {KINDS} 중 하나: {unknown}")
    made: dict[str, Recognizer] = {}

    def make(name: str) -> Recognizer:
        if name not in made:
            made[name] = get_recognizer(name, settings=settings, site=site)
        return made[name]

    default = make(settings.recognizer)
    if not by_kind_names:
        return default
    return ByKindRecognizer(default, {k: make(n) for k, n in by_kind_names.items()})


__all__ = ["CellContext", "Recognition", "Recognizer", "NullRecognizer", "OracleRecognizer", "ByKindRecognizer",
           "REGISTRY", "KINDS", "register", "available", "get_recognizer", "build_recognizer", "load_answers_json", "spec_for"]
