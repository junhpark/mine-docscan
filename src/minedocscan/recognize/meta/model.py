"""메타 필드 모델 폴더 읽기와 한 칸 읽기 — torch 없이 (tasks/0004 4.1, 4.2, 4.7).

모델 폴더(<site>/models/<이름>/): model.onnx, card.json, classes.json (+ train-log.jsonl).
  card.json     0003 의 카드 항목 + "meta": 읽는 법(reader: digits | choice), 키, 종류의 수·종류별 개수 분포(값 없이), 기준을 정한 방식(cv)
  classes.json  후보의 값 — 숫자 모델은 키마다 학습 때 본 값, 분류기는 종류(이름) 목록. **이름·차량번호가 들어 있다** — 사이트 팩에만

후보 목록(4.2) = 모델이 학습 때 본 값(classes.json) + 템플릿에 인쇄된 값(행렬 머리글의 header_<키>). 검수와 무관하다 —
검수를 저장할 때 목록이 바뀌면 다른 쪽의 기계 값이 달라져 "기계 값은 검수가 건드리지 않는다"(ADR 0008)가 깨진다. 날짜의 부분은
월 1–12, 일 1–31.

값(이름·차량번호)은 로그·오류 메시지에 찍지 않는다.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from ...config import ConfigError
from ...forms.template import DATE_PARTS
from ...imaging.cropspec import CropSpec
from .calib import status_of
from .choose import Choice, choose, readable

READERS = ("digits", "choice")
FIXED_VALUES = {"date.month": [str(m) for m in range(1, 13)], "date.day": [str(d) for d in range(1, 32)]}


class MetaModelError(ValueError):
    pass


def template_values(site, key: str) -> list[str]:
    """템플릿에 인쇄된 그 키의 값 (행렬 양식 열의 header_<키>). 순서는 템플릿·열 순서, 중복 없이."""
    out: list[str] = []
    if site is None:
        return out
    for t in site.templates.values():
        for reg in t.regions:
            for c in reg["columns"]:
                v = c.get(f"header_{key}")
                if v not in (None, "") and str(v) not in out:
                    out.append(str(v))
    return out


def is_meta_card(model_dir: str | Path) -> bool:
    try:
        return "meta" in json.loads((Path(model_dir) / "card.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


class MetaModel:
    """메타 필드 모델 하나 (한 폴더). 키가 여럿일 수 있다 (예: 숫자 모델 하나가 date.month, date.day)."""

    def __init__(self, model_dir: str | Path):
        self.dir = Path(model_dir)
        card = json.loads((self.dir / "card.json").read_text(encoding="utf-8"))
        meta = card.get("meta")
        if not meta:
            raise MetaModelError(f"메타 필드 모델이 아닙니다 (카드에 meta 가 없다 — 숫자 칸 모델이면 [recognize.digits] 에): {self.dir}")
        self.reader = meta.get("reader")
        if self.reader not in READERS:
            raise MetaModelError(f"모델 카드의 읽는 법이 {READERS} 가 아닙니다: {self.reader!r} ({self.dir})")
        self.keys = list(meta.get("keys") or [])
        cls_path = self.dir / "classes.json"
        if not cls_path.is_file():
            raise MetaModelError(f"classes.json 이 없습니다: {self.dir}")
        self.classes = json.loads(cls_path.read_text(encoding="utf-8"))
        if self.reader == "digits":
            from ..digits.model import OnnxNet, load_card

            self.card = load_card(self.dir)                      # 문자·입력·SHA-256 확인
            self.net = OnnxNet(self.dir / "model.onnx")
        else:
            from ..choice.model import ChoiceNet
            from ..choice.model import load_card as load_choice_card

            self.card = load_choice_card(self.dir)
            self.net = ChoiceNet(self.dir / "model.onnx", self.card)
            if len(self.classes.get("classes", [])) != self.card["meta"]["classes"]["n"]:
                raise MetaModelError(f"classes.json 의 종류 수가 카드와 다릅니다: {self.dir}")
        self.name = self.card["name"]
        self.position = bool(self.card["input"].get("position", True))      # 숫자 모델의 위치 채널 (메타 필드 모델은 끈다)
        self.spec = CropSpec.from_dict(self.card["spec"])
        self.temperature = float(self.card["temperature"])
        t = (self.card.get("auto_accept") or {}).get("threshold")
        self.threshold = math.inf if t is None else float(t)

    def values(self, key: str) -> list[str]:
        """학습 때 본 값 (숫자 모델: 키마다, 분류기: 종류)."""
        if self.reader == "choice":
            return list(self.classes.get("classes", []))
        if key in FIXED_VALUES:
            return list(FIXED_VALUES[key])
        return list((self.classes.get("values") or {}).get(key, []))

    def candidates(self, key: str, site=None) -> list[str]:
        """기계가 고를 후보 = 학습 때 본 값 + 템플릿에 인쇄된 값 (숫자 모델은 숫자만). 검수와 무관하다 (4.2)."""
        vals = self.values(key)
        if self.reader == "digits" and key not in DATE_PARTS:
            vals = vals + [v for v in template_values(site, key) if v not in vals]
            vals = [v for v in vals if readable(v)]
        return vals

    def read(self, crop: np.ndarray, key: str, candidates: list[str]) -> Choice:
        if self.reader == "digits":
            from ..digits.model import preprocess

            return choose(self.net.logits(preprocess(crop, self.position)), key, candidates, self.temperature)
        return self.net.choose(crop, candidates, self.temperature)

    def status(self, c: Choice) -> str:
        return status_of(c.answer, c.confidence, self.threshold)

    def describe(self) -> dict:
        aa = self.card.get("auto_accept") or {}
        m = self.card["meta"]
        return {"model": self.name, "path": str(self.dir), "reader": self.reader, "keys": self.keys,
                "spec": self.spec.describe(), "auto_accept_conf": None if math.isinf(self.threshold) else self.threshold,
                "auto_accept_upper95": aa.get("upper95"), "auto_accept_reason": aa.get("reason"),
                "method": (m.get("cv") or {}).get("method", "val_dates"), "classes": m.get("classes")}


# [recognize.meta] 에 쓰면 오류인 항목 — 숫자 칸 모델([recognize.digits])에는 있는 설정이라 헷갈려 쓰기 쉽다
NOT_MODEL_KEYS = ("auto_accept_conf", "model", "threshold")


def build_meta_readers(settings, site) -> dict[str, MetaModel]:
    """설정 [recognize.meta] <키> = "<모델 이름|경로>" (설정 파일 > site.toml). 같은 모델 폴더는 한 번만 읽는다.
    카드에 그 키가 없으면 시작할 때 오류 — 다른 키에 꽂은 모델은 받지 않는다."""
    from ..digits.model import resolve_model

    conf = _flatten(dict((getattr(settings, "recognizer_options", None) or {}).get("meta", {}) or {}))
    if site is not None:
        conf = _flatten(dict(site.option("recognize", "meta", {}) or {})) | conf
    for key, ref in sorted(conf.items()):
        # [recognize.meta] 의 항목은 "키 = 모델 이름" 뿐이다. 기준 같은 다른 항목은 조용히 무시되던 것을 설정 오류로 (tasks/0005 단계 1)
        if key in NOT_MODEL_KEYS:
            raise ConfigError(f"[recognize.meta] {key}: 이 표에는 \"메타 키 = 모델 이름\" 만 씁니다 — 메타 필드 모델의 자동 적재 "
                              "기준은 모델 카드에서만 옵니다 (기준을 바꾸려면 --target-auto-error 로 다시 학습)")
        if not isinstance(ref, str) or not ref:
            raise ConfigError(f"[recognize.meta] {key} 는 모델 이름(문자열)이어야 합니다: {ref!r}")
    out: dict[str, MetaModel] = {}
    loaded: dict[Path, MetaModel] = {}
    site_root = site.root if site is not None else getattr(settings, "site", None)
    for key, ref in sorted(conf.items()):
        try:
            d = resolve_model(ref, site_root)
        except FileNotFoundError as e:
            raise MetaModelError(f"[recognize.meta] {key}: {e}") from e
        if d.resolve() not in loaded:
            loaded[d.resolve()] = MetaModel(d)
        m = loaded[d.resolve()]
        if key not in m.keys:
            raise MetaModelError(f"[recognize.meta] {key} 에 모델 '{m.name}' 을 꽂을 수 없습니다 — 이 모델은 "
                                 f"{', '.join(m.keys) or '(키 없음)'} 를 읽는다 ({m.reader})")
        out[key] = m
    return out


def _flatten(d: dict, prefix: str = "") -> dict:
    """TOML 에서 따옴표 없이 date.day = "…" 라고 쓰면 {"date": {"day": …}} 가 된다 — 점으로 이은 키로 편다."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out |= _flatten(v, f"{prefix}{k}.")
        else:
            out[f"{prefix}{k}"] = v
    return out
