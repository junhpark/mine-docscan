"""모델이 필요 없는 기본 백엔드 두 개."""
from __future__ import annotations

import json
from pathlib import Path

from .base import CellContext, Recognition

AnswerKey = tuple[str, str, str, str, str]


class NullRecognizer:
    """인식기가 없을 때의 자리표시자: 빈 텍스트, 신뢰도 0 → 잉크가 있는 셀은 전부 검수 대상이 된다."""

    name = "null"

    def recognize(self, crops, contexts: list[CellContext]) -> list[Recognition]:
        return [Recognition("", 0.0, [], self.name) for _ in contexts]


class OracleRecognizer:
    """정답을 그대로 돌려주는 시험용 백엔드.

    인식기를 뺀 나머지 파이프라인(정합·셀 추출·검증·적재·평가)이 맞게 동작하는지 확인할 때 쓴다.
    이 백엔드로 돌렸을 때 CER 이 0 이 아니면 인식기가 아니라 파이프라인에 버그가 있는 것이다.

    answers 키: (출처, template, region, field_name, row_key). 출처는 둘 중 하나다.
      · "<파일명>#<페이지>"  — 같은 날 같은 양식이 여러 장일 때 (차량별 일보)
      · "YYYY-MM-DD"         — 그날 그 양식이 한 장뿐일 때 (점검표)
    페이지 출처를 먼저 찾고, 없으면 날짜로 찾는다.
    """

    name = "oracle"

    def __init__(self, answers: dict[AnswerKey, str] | None = None):
        self.answers = answers or {}

    def recognize(self, crops, contexts: list[CellContext]) -> list[Recognition]:
        out = []
        for c in contexts:
            tail = (c.template, c.region, c.field_name, c.row_key)
            text = self.answers.get((c.source, *tail))
            if text is None and c.work_date:
                text = self.answers.get((c.work_date, *tail))
            if text is None:
                out.append(Recognition("", 0.0, [], self.name))
            else:
                out.append(Recognition(text, 1.0, [], self.name))
        return out


def load_answers_json(path: str | Path) -> dict[AnswerKey, str]:
    """정답 JSON → answers. 형식: [{source | work_date, template, region, field_name, row_key, text}, …]"""
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[AnswerKey, str] = {}
    for a in items:
        origin = a.get("source") or a.get("work_date")
        if not origin:
            raise ValueError(f"정답 항목에 source 도 work_date 도 없습니다: {a}")
        out[(origin, a["template"], a["region"], a["field_name"], str(a.get("row_key", "")))] = a["text"]
    return out
