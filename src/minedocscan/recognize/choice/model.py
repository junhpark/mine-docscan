"""분류기의 추론 쪽 — torch 없이 (tasks/0004 4.1). 학습(train.py)이 ONNX 로 내보낸 파일을 OpenCV(cv2.dnn)로 읽는다.

입력: 크롭(회색조) → INPUT_W × INPUT_H 로 줄이고 → 종이·잉크로 밝기를 맞춘 잉크 채널 하나 (숫자 모델의 normalize 와 같은 규칙).
출력: 종류마다 점수 (종류 0 = "그 밖", 1… = classes.json 의 종류 순서). 신뢰도 = 온도로 보정한 softmax 확률.
답: 가장 높은 종류. "그 밖"이면 거절(reject — 검수로 간다).
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from ..digits.model import sha256_file
from ..meta.choose import Choice

INPUT_W, INPUT_H = 128, 40
N_INPUT_CHANNELS = 1
OTHER = 0                                    # "그 밖" 종류의 번호
CARD_KEYS = ("name", "created_at", "code_version", "spec", "input", "architecture", "train_args", "seed", "data",
             "validation", "temperature", "auto_accept", "libraries", "model_sha256", "meta")


def resize_input(gray: np.ndarray) -> np.ndarray:
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    h, w = gray.shape
    if (w, h) == (INPUT_W, INPUT_H):
        return gray.copy()
    shrink = w > INPUT_W or h > INPUT_H
    return cv2.resize(gray, (INPUT_W, INPUT_H), interpolation=cv2.INTER_AREA if shrink else cv2.INTER_LINEAR)


def normalize(small: np.ndarray) -> np.ndarray:
    """(H, W) uint8 → (1, H, W) float32 잉크 채널 (digits/model.normalize 와 같은 밝기 맞추기)."""
    g = small.astype(np.float32)
    paper = float(np.median(g))
    dark = float(np.percentile(g, 1))
    return np.clip((paper - g) / max(paper - dark, 48.0), -0.3, 1.2)[None].astype(np.float32)


def preprocess(crop: np.ndarray) -> np.ndarray:
    return normalize(resize_input(crop))[None]


def softmax(z: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    z = np.asarray(z, np.float64) / float(temperature)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def load_card(model_dir: str | Path, check_hash: bool = True) -> dict:
    d = Path(model_dir)
    card = json.loads((d / "card.json").read_text(encoding="utf-8"))
    lacking = [k for k in CARD_KEYS if k not in card]
    if lacking:
        raise ValueError(f"모델 카드에 항목이 없습니다: {lacking} ({d / 'card.json'})")
    inp = card["input"]
    if (inp.get("width"), inp.get("height"), inp.get("channels")) != (INPUT_W, INPUT_H, N_INPUT_CHANNELS):
        raise ValueError(f"모델 카드의 입력({inp})이 이 코드의 입력({INPUT_W}×{INPUT_H}×{N_INPUT_CHANNELS})과 다릅니다")
    if check_hash and sha256_file(d / "model.onnx") != card["model_sha256"]:
        raise ValueError(f"model.onnx 가 카드에 적힌 것(SHA-256)과 다릅니다: {d}")
    return card


class ChoiceNet:
    """model.onnx (배치 1) + classes.json 의 종류."""

    def __init__(self, path: str | Path, card: dict | None = None):
        self.path = Path(path)
        self.net = cv2.dnn.readNetFromONNX(str(self.path))
        cls = json.loads((self.path.parent / "classes.json").read_text(encoding="utf-8"))
        self.classes: list[str] = list(cls.get("classes", []))

    def logits(self, x: np.ndarray) -> np.ndarray:
        """(1, 1, H, W) → (종류 수 + 1,)"""
        self.net.setInput(np.ascontiguousarray(x, dtype=np.float32))
        return np.asarray(self.net.forward()).reshape(-1)

    def logits_many(self, xs: np.ndarray) -> np.ndarray:
        return np.stack([self.logits(x[None]) for x in xs]) if len(xs) else np.zeros((0, len(self.classes) + 1), np.float32)

    def choose(self, crop: np.ndarray, candidates: list[str] | None = None, temperature: float = 1.0) -> Choice:
        return choose_from_logits(self.logits(preprocess(crop)), self.classes, temperature)


def choose_from_logits(z: np.ndarray, classes: list[str], temperature: float = 1.0) -> Choice:
    """점수 → Choice. 가장 높은 것이 "그 밖"이면 거절, 아니면 그 종류 (신뢰도 = 그 확률)."""
    p = softmax(z, temperature)
    order = [int(i) for i in np.argsort(-p)]
    top = order[0]
    names = [classes[i - 1] for i in order if i != OTHER][:5]
    if top == OTHER:
        return Choice("?", float(p[top]), "reject", names, "")
    return Choice(classes[top - 1], float(p[top]), "value", names, classes[top - 1])


def truth_prob(z: np.ndarray, classes: list[str], truth: str, temperature: float) -> float | None:
    if truth not in classes:
        return None
    return float(softmax(z, temperature)[classes.index(truth) + 1])
