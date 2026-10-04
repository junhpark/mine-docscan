"""인식 백엔드 "digits" — 숫자 칸(handwritten_number)만 읽는다 (tasks/0003 단계 5).

모델 폴더(model.onnx + card.json)를 읽어 카드의 규격·온도·자동 적재 기준을 그대로 쓴다. 규격은 설정으로 따로 주지 않는다
— 모델이 학습한 규격을 백엔드가 선언한다 (4.1). 추론은 OpenCV(cv2.dnn)뿐이다. torch 를 import 하지 않는다.

답(Recognition): text = 숫자열 | "" | "?", confidence = 그 답의 확률(온도 보정), candidates = 상위 5개 답,
answer = value | empty | reject, threshold = 자동 적재 기준. 표(4.4)를 적용하는 것은 핸들러다 (handlers/base.number_status).
선언한 종류가 아닌 칸이 오면 읽지 않고 신뢰도 0 의 빈 답을 돌려준다 (예전 규칙 → 검수 대기).
"""
from __future__ import annotations

import math
from pathlib import Path

from ...imaging.cropspec import CropSpec
from ..base import CellContext, Recognition
from .model import (
    KINDS,
    OnnxNet,
    answer_kind,
    card_threshold,
    load_card,
    preprocess,
    read_answers,
    resolve_model,
)


class DigitsRecognizer:
    name = "digits"

    def __init__(self, model_dir: str | Path, threshold: float | None = None, threshold_source: str = "card",
                 fallback_threshold: float = 0.90):
        self.model_dir = Path(model_dir)
        self.card = load_card(self.model_dir)
        if self.card.get("meta"):
            raise ValueError(f"'{self.card['name']}' 은 메타 필드 모델입니다 ({', '.join(self.card['meta'].get('keys', []))}) — "
                             "[recognize.digits] 가 아니라 [recognize.meta] 에 꽂으세요")
        self.net = OnnxNet(self.model_dir / "model.onnx")
        self.spec = CropSpec.from_dict(self.card["spec"])
        self.temperature = float(self.card["temperature"])
        if threshold is not None:
            self.threshold, self.threshold_source = float(threshold), threshold_source
        elif "met" in (self.card.get("auto_accept") or {}):
            self.threshold, self.threshold_source = card_threshold(self.card), "card"
        else:                                                       # 카드에 기준이 없다 → [pipeline] auto_accept_conf
            self.threshold, self.threshold_source = float(fallback_threshold), "pipeline"

    @classmethod
    def from_settings(cls, settings=None, site=None) -> DigitsRecognizer:
        """모델: 설정 [recognize.digits] model > site.toml 의 같은 항목 (4.5). 기준: 설정 [recognize.digits] auto_accept_conf >
        모델 카드 > [pipeline] auto_accept_conf (4.6)."""
        opts = dict((getattr(settings, "recognizer_options", None) or {}).get("digits", {}) or {})

        def site_opt(key):
            return site.option("recognize.digits", key) if site is not None else None

        ref = opts.get("model") or site_opt("model")
        if not ref:
            raise ValueError('digits 백엔드에 모델이 지정되지 않았습니다: 설정 파일(또는 site.toml)에 '
                             '[recognize.digits] model = "<이름>" — 사이트 팩의 모델은 `minedocscan recognizer list`')
        site_root = site.root if site is not None else getattr(settings, "site", None)
        model_dir = resolve_model(str(ref), site_root)
        thr = opts.get("auto_accept_conf")
        fallback = float(getattr(settings, "auto_accept_conf", 0.90))
        return cls(model_dir, None if thr is None else _conf(thr), "config", fallback)

    def crop_spec_for(self, kind: str) -> CropSpec | None:
        return self.spec if kind in KINDS else None

    def recognize(self, crops, contexts: list[CellContext]) -> list[Recognition]:
        out = []
        for crop, ctx in zip(crops, contexts, strict=True):
            if ctx.kind not in KINDS:
                out.append(Recognition("", 0.0, [], self.name))
                continue
            answers = read_answers(self.net.logits(preprocess(crop)), self.temperature)
            text, p = answers[0]
            out.append(Recognition(text, float(p), [a for a, _p in answers], self.name,   # 반올림하지 않는다 (기준과 그대로 비교)
                                   answer=answer_kind(text), threshold=self.threshold))
        return out

    def describe(self) -> dict:
        c = self.card
        return {"backend": self.name, "model": c["name"], "path": str(self.model_dir), "spec": self.spec.describe(),
                "auto_accept_conf": None if math.isinf(self.threshold) else self.threshold,
                "auto_accept_source": self.threshold_source, "temperature": self.temperature,
                # 카드의 기준을 쓸 때만: 검증에서 그 기준이 말해 주는 자동 적재 오류율의 95 % 상한 (설정으로 준 기준에는 근거가 없다)
                "auto_accept_upper95": (c.get("auto_accept") or {}).get("upper95") if self.threshold_source == "card" else None,
                "auto_accept_reason": (c.get("auto_accept") or {}).get("reason") if self.threshold_source == "card" else None,
                "train_cells": c["data"]["train"]["cells"], "synthetic_cells": c["data"]["synthetic"]["cells"],
                "created_at": c["created_at"]}


def _conf(v) -> float:
    t = float(v)
    if not 0.0 <= t <= 1.0:
        raise ValueError(f"[recognize.digits] auto_accept_conf 는 0 이상 1 이하: {v}")
    return t
