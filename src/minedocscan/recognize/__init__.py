"""인식 백엔드 등록소. 새 백엔드는 register() 로 등록하고 설정으로 고른다.

  [recognize] backend = "null"                  기본 백엔드
  [recognize.by_kind] handwritten_number = "…"  칸 종류(handwritten_number, handwritten_text)마다 다른 백엔드
  [recognize.by_format] reading = "…"           값의 형식(decimal, time, time_range, reading)마다 — 적힌 형식의 칸만 읽는다 (tasks/0011 4.1)

우선순위: by_format > by_kind > backend. 파이프라인은 여전히 Recognizer 하나만 안다 — 형식·종류를 보고 나눠 주는 것도
Recognizer(ByKindRecognizer)다. 형식 칸을 인식기에 보내는가는 handlers/base.reads_format 이 그 인식기의 형식 표로 정한다 —
감싸지 않은 인식기(null·oracle·digits)는 형식 칸을 읽지 않는다.
백엔드는 읽는 형식을 선언한다 (`formats` — 없으면 읽지 않는다). 선언은 검사에만 쓴다: 선언하지 않은 형식에 꽂으면 만들 때 멈춘다.
백엔드의 팩토리는 settings·site 를 키워드로 받을 수 있다 (받지 않는 팩토리에는 넘기지 않는다).
"""
from __future__ import annotations

import inspect
from collections.abc import Callable

from ..forms.formats import FORMATS
from .base import CellContext, Recognition, Recognizer, spec_for
from .builtin import NullRecognizer, OracleRecognizer, load_answers_json

REGISTRY: dict[str, Callable[..., Recognizer]] = {
    "null": NullRecognizer,
    "oracle": OracleRecognizer,
}

KINDS = ("handwritten_number", "handwritten_text")
# by_format 이 받는 형식 — 정수 칸(integer)은 숫자 경로(by_kind)다
READ_FORMATS = tuple(f for f in FORMATS if f != "integer")


def register(name: str, factory: Callable[..., Recognizer]) -> None:
    REGISTRY[name] = factory


def _ensure_plugins() -> None:
    """저장소 안의 선택 백엔드를 등록한다 (무거운 의존성은 각 모듈 안에서만 import 한다)."""
    from .digits import make as make_digits
    from .readings import make as make_readings

    REGISTRY.setdefault("digits", make_digits)
    REGISTRY.setdefault("readings", make_readings)


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
    """칸의 형식·종류마다 다른 백엔드로 나눠 준다 (형식 표 by_format 이 먼저, 그다음 종류 표 by_kind, 그다음 기본).
    각 백엔드의 Recognition.backend 가 그대로 doc_field.backend 에 남는다."""

    name = "by_kind"

    def __init__(self, default: Recognizer, by_kind: dict[str, Recognizer],
                 by_format: dict[str, Recognizer] | None = None):
        self.default = default
        self.by_kind = dict(by_kind)
        self.by_format = dict(by_format or {})

    def backend_for(self, kind: str, fmt: str | None = None) -> Recognizer:
        if fmt is not None and fmt in self.by_format:
            return self.by_format[fmt]
        return self.by_kind.get(kind, self.default)

    def crop_spec_for(self, kind: str, fmt: str | None = None):
        return spec_for(self.backend_for(kind, fmt), kind, fmt)

    def recognize(self, crops, contexts: list[CellContext]) -> list[Recognition]:
        out: list[Recognition | None] = [None] * len(contexts)
        groups: dict[int, list[int]] = {}
        backends: dict[int, Recognizer] = {}
        for i, c in enumerate(contexts):
            b = self.backend_for(c.kind, c.fmt)
            groups.setdefault(id(b), []).append(i)
            backends[id(b)] = b
        for key, idx in groups.items():
            recs = backends[key].recognize([crops[i] for i in idx], [contexts[i] for i in idx])
            for i, r in zip(idx, recs, strict=True):
                out[i] = r
        return out                                                # type: ignore[return-value]

    def describe(self) -> dict:
        d = {"default": self.default.name, "by_kind": {k: b.name for k, b in self.by_kind.items()}}
        if self.by_format:
            d["by_format"] = {f: b.name for f, b in self.by_format.items()}
        return d


def declared_formats(backend) -> tuple[str, ...]:
    """백엔드가 읽는다고 선언한 형식 (`formats`). 선언하지 않았으면 없다."""
    return tuple(getattr(backend, "formats", ()) or ())


def check_formats(names) -> None:
    """[recognize.by_format] 의 형식 이름 검사. 정수(integer)는 받지 않는다 — 정수 칸은 [recognize.by_kind]."""
    bad = sorted(set(names) - set(READ_FORMATS))
    if bad:
        hint = " — 정수 칸은 [recognize.by_kind] 로" if "integer" in bad else ""
        raise ValueError(f"[recognize.by_format] 의 형식은 {', '.join(READ_FORMATS)} 중 하나: {', '.join(map(str, bad))}{hint}")


def by_formats(recognizer: Recognizer, formats) -> Recognizer:
    """이미 만든 인식기 하나(정답 oracle — run --answers)를 적힌 형식들의 표로 감싼다 (tasks/0011 4.1). 형식이 없으면 그대로."""
    formats = list(formats or ())
    if not formats:
        return recognizer
    check_formats(formats)
    return ByKindRecognizer(recognizer, {}, {f: recognizer for f in formats})


def build_recognizer(settings, site=None) -> Recognizer:
    """설정에서 인식기를 만든다: [recognize] backend 가 기본, [recognize.by_kind] 가 종류별, [recognize.by_format] 이 형식별.
    by_kind·by_format 이 둘 다 없으면 기본 백엔드 하나를 그대로 돌려준다 (지금까지와 같다)."""
    by_kind_names = dict(getattr(settings, "recognizer_by_kind", {}) or {})
    by_format_names = dict(getattr(settings, "recognizer_by_format", {}) or {})
    unknown = sorted(set(by_kind_names) - set(KINDS))
    if unknown:
        raise ValueError(f"[recognize.by_kind] 의 종류는 {KINDS} 중 하나: {unknown}")
    check_formats(by_format_names)
    made: dict[str, Recognizer] = {}

    def make(name: str) -> Recognizer:
        if name not in made:
            made[name] = get_recognizer(name, settings=settings, site=site)
        return made[name]

    default = make(settings.recognizer)
    if not by_kind_names and not by_format_names:
        return default
    def refuse(fmt: str, name: str, declared) -> None:
        what = f"{fmt} 형식을 읽지 않습니다 (읽는 형식: {', '.join(declared)})" if declared else "소수·시각을 읽지 않습니다"
        raise ValueError(f"{name} 는 {what} — [recognize.by_format] {fmt}")

    by_format = {}
    for fmt, name in by_format_names.items():
        # 팩토리가 형식을 미리 말하면(클래스·숫자 모델) 만들기 전에 본다 — 모델이 없다는 엉뚱한 말 대신. 모델 카드가 정하는 백엔드는 만든 뒤에
        _ensure_plugins()
        pre = getattr(REGISTRY.get(name), "formats", None)
        if pre is not None and fmt not in pre:
            refuse(fmt, name, tuple(pre))
        b = make(name)
        if fmt not in declared_formats(b):
            refuse(fmt, name, declared_formats(b))
        by_format[fmt] = b
    return ByKindRecognizer(default, {k: make(n) for k, n in by_kind_names.items()}, by_format)


__all__ = ["CellContext", "Recognition", "Recognizer", "NullRecognizer", "OracleRecognizer", "ByKindRecognizer",
           "REGISTRY", "KINDS", "READ_FORMATS", "register", "available", "get_recognizer", "build_recognizer", "by_formats",
           "check_formats", "declared_formats", "load_answers_json", "spec_for"]
