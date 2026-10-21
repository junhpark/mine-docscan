"""인식 백엔드 "readings" — 소수·시각 칸(decimal·time·time_range·reading)을 읽는다 (tasks/0011 4.3).

모델 폴더(model.onnx + card.json)를 읽어 카드의 규격·글씨 띠·온도·자동 적재 기준을 그대로 쓴다. 추론은 OpenCV(cv2.dnn)뿐이다.
읽는 형식 = 카드의 formats (선언 — [recognize.by_format] 에 꽂을 수 있는 형식). 칸을 읽게 하는 것은 [recognize.by_format] 이다.

답(Recognition): text = 적힌 글자 | "" | "?", confidence = 그 값의 확률(온도 보정), candidates = 상위 5개 답의 적힌 글자,
answer = value | empty | reject, threshold = 자동 적재 기준. 표(4.2)를 적용하는 것은 핸들러다 (handlers/base.format_row).
형식이 없거나 선언하지 않은 형식의 칸, 칸 자리(bbox)를 모르는 칸이 오면 읽지 않고 신뢰도 0 의 빈 답 (숫자 모델이 모르는 종류에 하듯이
— 핸들러에서 검수 대기).
"""
from __future__ import annotations

import math
from pathlib import Path

from ...imaging.cropspec import CropSpec
from ..base import CellContext, Recognition
from ..digits.model import OnnxNet, card_threshold, resolve_model
from .model import N_CLASSES, cell_in_crop, decode, load_card, preprocess


class ReadingsRecognizer:
    name = "readings"

    def __init__(self, model_dir: str | Path, threshold: float | None = None, threshold_source: str = "card",
                 fallback_threshold: float = 0.90):
        self.model_dir = Path(model_dir)
        self.card = load_card(self.model_dir)
        self.formats = tuple(self.card["formats"])
        self.net = OnnxNet(self.model_dir / "model.onnx", N_CLASSES)
        self.spec = CropSpec.from_dict(self.card["spec"])
        self.temperature = float(self.card["temperature"])
        if threshold is not None:
            self.threshold, self.threshold_source = float(threshold), threshold_source
        elif "met" in (self.card.get("auto_accept") or {}):
            self.threshold, self.threshold_source = card_threshold(self.card), "card"
        else:
            self.threshold, self.threshold_source = float(fallback_threshold), "pipeline"

    @classmethod
    def from_settings(cls, settings=None, site=None) -> ReadingsRecognizer:
        """모델: 설정 [recognize.readings] model > site.toml 의 같은 항목. 기준: 설정 [recognize.readings] auto_accept_conf >
        모델 카드 > [pipeline] auto_accept_conf (tasks/0003 과 같다)."""
        opts = dict((getattr(settings, "recognizer_options", None) or {}).get("readings", {}) or {})

        def site_opt(key):
            return site.option("recognize.readings", key) if site is not None else None

        ref = opts.get("model") or site_opt("model")
        if not ref:
            raise ValueError('readings 백엔드에 모델이 지정되지 않았습니다: 설정 파일(또는 site.toml)에 '
                             '[recognize.readings] model = "<이름>" — 사이트 팩의 모델은 `minedocscan recognizer list`')
        site_root = site.root if site is not None else getattr(settings, "site", None)
        model_dir = resolve_model(str(ref), site_root)
        thr = opts.get("auto_accept_conf")
        if thr is not None:
            t = float(thr)
            if not 0.0 <= t <= 1.0:
                raise ValueError(f"[recognize.readings] auto_accept_conf 는 0 이상 1 이하: {thr}")
            thr = t
        return cls(model_dir, thr, "config", float(getattr(settings, "auto_accept_conf", 0.90)))

    def crop_spec_for(self, kind: str, fmt: str | None = None) -> CropSpec | None:
        return self.spec if fmt in self.formats else None

    def recognize(self, crops, contexts: list[CellContext]) -> list[Recognition]:
        out = []
        for crop, ctx in zip(crops, contexts, strict=True):
            if ctx.fmt not in self.formats or ctx.bbox is None:
                out.append(Recognition("", 0.0, [], self.name))
                continue
            x, _b = preprocess(crop, cell_in_crop(ctx.bbox, self.spec), float(self.spec.scale))
            answers = decode(self.net.logits(x), ctx.fmt, self.temperature)
            a = answers[0]
            out.append(Recognition(a.text, float(a.prob), [x.text for x in answers], self.name, answer=a.kind,
                                   threshold=self.threshold))
        return out

    def describe(self) -> dict:
        c = self.card
        return {"backend": self.name, "model": c["name"], "path": str(self.model_dir), "spec": self.spec.describe(),
                "formats": list(self.formats),
                "auto_accept_conf": None if math.isinf(self.threshold) else self.threshold,
                "auto_accept_source": self.threshold_source, "temperature": self.temperature,
                "auto_accept_upper95": (c.get("auto_accept") or {}).get("upper95") if self.threshold_source == "card" else None,
                "auto_accept_reason": (c.get("auto_accept") or {}).get("reason") if self.threshold_source == "card" else None,
                "train_cells": c["data"]["train"]["cells"], "synthetic_cells": c["data"]["synthetic"]["cells"],
                "created_at": c["created_at"]}
