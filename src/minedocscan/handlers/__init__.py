"""핸들러 등록소. 템플릿 YAML 의 `handler:` 값이 여기의 이름이다."""
from __future__ import annotations

from .base import FormHandler, PageContext
from .haul import HaulHandler
from .inspection import InspectionHandler
from .usage import UsageHandler

REGISTRY: dict[str, type[FormHandler]] = {
    "generic": FormHandler,
    "inspection": InspectionHandler,
    "haul": HaulHandler,
    "usage": UsageHandler,
}


def register(name: str, handler: type[FormHandler]) -> None:
    REGISTRY[name] = handler


def get_handler(name: str) -> FormHandler:
    if name not in REGISTRY:
        raise KeyError(f"등록되지 않은 핸들러: {name} (사용 가능: {sorted(REGISTRY)})")
    return REGISTRY[name]()


__all__ = ["FormHandler", "PageContext", "REGISTRY", "register", "get_handler"]
