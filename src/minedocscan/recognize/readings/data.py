"""소수·시각 모델의 학습·평가용 크롭 폴더 읽기 — torch 없이 (tasks/0011 4.3, 4.4, 4.6).

폴더는 숫자 칸과 같은 `review export-crops` 의 것이다 (한 번 내보내면 두 모델의 크롭이 나온다). 여기서는 형식이 넷(decimal·time·
time_range·reading) 중 하나인 **표의 칸** 줄만 쓴다 — 칸 종류는 묻지 않는다 (4.1 의 범위와 같다). 숫자 모델(digits/data.read_crops)은
같은 폴더에서 형식 칸 줄을 뺀다.

정답: 검수는 정규화한 값만 남긴다(review/store._save) — 종이의 글자가 아니다 (1절 다). 그래서 목표가 여럿이다:
  value → formats.spellings(형식, 값) (그 값이 될 수 있는, 사람이 종이에 쓰는 글자들 — 정규형부터, 많아야 128),
  empty → [""], illegible → ["?"]. 손실은 그 확률의 합 (−log Σ P(표기) — train.py, calib.py).
test 줄은 거절, 규격 하나, 검증 날짜는 숫자 모델과 같은 규칙(digits/data — 소금값 + ":val"). 값은 어디에도 찍지 않는다 — 모양(dddd.d)의 수만.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ...forms.formats import FormatError, normalize, spellings
from ...imaging.cropspec import CropSpec
from ...imaging.io import imread_gray
from ..digits.data import CropsError, _resolve, label_files
from .model import FORMATS, REJECT, cell_in_crop, shape_of

VERDICT_TEXT = {"empty": "", "illegible": REJECT}


@dataclass
class Sample:
    path: Path
    fmt: str
    value: str                      # 정답: 정규화한 값 | "" (빈 칸) | "?" (읽을 수 없음)
    verdict: str
    work_date: str | None
    bbox: tuple[int, int, int, int] | None
    cell: tuple[float, float, float, float]          # 크롭 안의 칸 자리 (크롭 화소)
    scale: float                                      # 템플릿 px 당 크롭 화소
    field_id: str = ""
    inked: bool = True              # 잉크 판정이 "값 있음"이었거나 인식기에 간 칸 (export-crops 의 inked — 4.6)
    targets: list[str] = field(default_factory=list)  # CTC 목표들 (value 의 spellings, [""], ["?"])

    def image(self) -> np.ndarray:
        return imread_gray(self.path)


@dataclass
class Crops:
    samples: list[Sample]
    spec: CropSpec | None
    lines: int
    skipped: dict[str, int]
    splits: set[str]


def targets_of(fmt: str, value: str) -> list[str]:
    """정답 → CTC 목표들 (4.4)."""
    if value in ("", REJECT):
        return [value]
    return spellings(fmt, value)


def value_of(line: dict) -> str | None:
    """판정 → 정답(정규화한 값 | "" | "?"). 형식에 맞지 않는 값(예전 검수 줄 등)은 None — 쓰지 않는다."""
    v = line.get("verdict", "value")
    if v in VERDICT_TEXT:
        return VERDICT_TEXT[v]
    try:
        return normalize(line.get("format"), str(line.get("text", "")))
    except FormatError:
        return None


def read_crops(root: str | Path, *, allow_test: bool = False, only_split: str | None = None) -> Crops:
    """폴더 → 형식 칸의 Crops. allow_test=False 면 test 줄이 하나라도 있으면 CropsError (학습). 규격이 둘 이상이면 CropsError."""
    root = Path(root)
    files = label_files(root)
    if not files:
        raise CropsError(f"labels.jsonl 이 없습니다: {root} — `minedocscan review export-crops` 로 내보낸 폴더를 주세요")
    samples: list[Sample] = []
    specs: set[CropSpec] = set()
    skipped: Counter = Counter()
    splits: set[str] = set()
    n = 0
    for lf in files:
        for k, raw in enumerate(lf.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            n += 1
            try:
                line = json.loads(raw)
            except json.JSONDecodeError as e:
                raise CropsError(f"{lf}:{k} 줄을 읽을 수 없습니다 ({e.msg})") from e
            sp = line.get("split", "unknown")
            splits.add(sp)
            if sp == "test" and not allow_test:
                raise CropsError(f"{lf} 에 test 로 나뉜 줄이 있습니다. test 는 마지막에 한 번 보는 것이라 학습에 쓰지 않습니다 "
                                 "— `review export-crops --split train` 으로 내보낸 폴더를 주세요 (tasks/0003 4.6)")
            if only_split and sp != only_split:
                skipped["other_split"] += 1
                continue
            if line.get("meta_key") or line.get("region") == "fields":   # 표 밖 필드의 형식 칸은 읽지 않는다 (4.1)
                skipped["meta_field"] += 1
                continue
            fmt = line.get("format")
            if fmt not in FORMATS:
                skipped["other_format"] += 1
                continue
            if "spec" not in line or not line.get("bbox"):
                raise CropsError(f"{lf}:{k} 에 크롭 규격(spec)·칸 자리(bbox)가 없습니다 — 이 버전의 `review export-crops` 로 "
                                 "다시 내보내세요")
            spec = CropSpec.from_dict(line["spec"])
            specs.add(spec)
            value = value_of(line)
            if value is None:
                skipped["not_a_value"] += 1
                continue
            bbox = tuple(int(v) for v in line["bbox"])
            samples.append(Sample(_resolve(root, lf, line["file"]), fmt, value, line.get("verdict", "value"),
                                  line.get("work_date"), bbox, cell_in_crop(bbox, spec), float(spec.scale),
                                  line.get("field_id", ""), bool(line.get("inked", True)), targets_of(fmt, value)))
    if len(specs) > 1:
        raise CropsError(f"크롭 규격이 한 가지가 아닙니다 ({len(specs)}가지: "
                         f"{', '.join(sorted(s.describe() for s in specs))}). 규격마다 따로 내보내고 따로 학습하세요")
    missing = sum(not s.path.is_file() for s in samples)
    if missing:
        raise CropsError(f"labels.jsonl 에 적힌 그림 {missing}개가 없습니다 (폴더를 옮겼거나 일부만 복사했습니다): {root}")
    return Crops(samples, next(iter(specs)) if specs else None, n, dict(skipped), splits)


def summarize(samples: list[Sample]) -> dict:
    """카드에 적는 요약: 칸 수, 날짜 수, 판정별·형식별 개수, 값 대신 **모양**의 분포 (숫자를 d 로 — dddd.d: 18)."""
    by_verdict = Counter(s.verdict for s in samples)
    by_format = Counter(s.fmt for s in samples)
    by_shape = Counter(f"{s.fmt}:{shape_of(s.value)}" for s in samples if s.value not in ("", REJECT))
    return {"cells": len(samples), "dates": len({s.work_date for s in samples if s.work_date}),
            "by_verdict": dict(sorted(by_verdict.items())), "by_format": dict(sorted(by_format.items())),
            "by_shape": dict(sorted(by_shape.items()))}
