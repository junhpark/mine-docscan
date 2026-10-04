"""학습·평가용 크롭 폴더 읽기와 검증 날짜 — torch 없이 (tasks/0003 4.6).

폴더는 `review export-crops` 가 만든 것이다: 어딘가에 labels.jsonl 이 있고, 줄마다 file(내보낸 폴더 기준 경로), text,
verdict, kind, work_date, split, spec. 판정 → 정답: value → 숫자열(앞의 0 을 뗀다), empty → "", illegible → "?".

test 는 마지막에 한 번 보는 것이다: 학습은 split 이 test 인 줄이 하나라도 있으면 거절한다.
검증(val)은 train 날짜 안에서 날짜 단위로 뗀다. 어느 날짜가 검증인지는 날짜와 소금값으로만 정해진다
(evaluate/split.py 와 같은 규칙에 소금값 뒤에 ":val" 을 붙인 것 — test 분할과 서로 독립이다).

여기서 다루는 값(셀 값)은 로그·오류 메시지에 찍지 않는다. 집계(값별 개수)만 낸다 (4.7).
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ...evaluate.split import split_of
from ...imaging.cropspec import CropSpec
from ...imaging.io import imread_gray
from .model import KINDS, REJECT, normalize_answer

VERDICT_TEXT = {"empty": "", "illegible": REJECT}


class CropsError(ValueError):
    """크롭 폴더를 쓸 수 없다 (test 가 섞였다, 규격이 섞였다, 비었다 …)."""


@dataclass
class Sample:
    path: Path
    text: str                       # 정답: 숫자열 | "" | "?"
    verdict: str
    work_date: str | None
    bbox: tuple[int, int, int, int] | None
    field_id: str = ""
    inked: bool = True              # 잉크 판정이 "값 있음"이던 칸 (파이프라인이 인식기에 보내는 칸). 예전 라벨에는 없다 → True
    meta_key: str = ""              # 메타 필드 줄이면 그 키 (vehicle_no, operator, date.day …)

    def image(self) -> np.ndarray:
        return imread_gray(self.path)


@dataclass
class Crops:
    samples: list[Sample]
    spec: CropSpec | None
    lines: int
    skipped: dict[str, int]
    splits: set[str]


def label_files(root: str | Path) -> list[Path]:
    root = Path(root)
    if root.is_file() and root.name == "labels.jsonl":
        return [root]
    return sorted(root.rglob("labels.jsonl"))


def _resolve(root: Path, labels: Path, rel: str) -> Path:
    """file 은 내보낸 폴더(OUT) 기준이다 (OUT/<split>/labels.jsonl, 메타는 OUT/<split>/meta/labels.jsonl).
    사용자가 OUT 을 줬든 OUT/<split> 을 줬든 찾는다."""
    for base in (labels.parent.parent, root, labels.parent, labels.parent.parent.parent):
        p = base / rel
        if p.is_file():
            return p
    return labels.parent.parent / rel


def text_of(line: dict) -> str | None:
    """판정 → 정답. 숫자가 아닌 값(문자를 적은 검수 등)은 None (쓰지 않는다)."""
    v = line.get("verdict", "value")
    if v in VERDICT_TEXT:
        return VERDICT_TEXT[v]
    t = str(line.get("text", "")).strip()
    return normalize_answer(t) if t.isdigit() else None


def meta_text_of(line: dict) -> str | None:
    """메타 필드 줄의 정답: 값 그대로 (차량번호의 앞 0 도 번호다, 이름은 이름). illegible → "?" (숫자 모델의 거절),
    empty → "". 숫자인지는 읽는 쪽이 본다 (숫자 모델은 숫자만, 분류기는 이름도)."""
    v = line.get("verdict", "value")
    if v in VERDICT_TEXT:
        return VERDICT_TEXT[v]
    t = str(line.get("text", "")).strip()
    return t or None


def read_crops(root: str | Path, *, allow_test: bool = False, only_split: str | None = None,
               kinds: tuple[str, ...] = KINDS, meta_keys: tuple[str, ...] | None = None) -> Crops:
    """폴더 → Crops. allow_test=False 면 test 줄이 하나라도 있으면 CropsError (학습). 규격이 둘 이상이면 CropsError.
    only_split: 그 분할의 줄만 (평가). kinds: 이 종류의 칸만 (숫자 칸).
    meta_keys: None 이면 표의 칸만 — 메타 필드 줄(meta_key 가 있거나 region 이 fields)은 뺀다. 주면 그 키의 메타 필드 줄만
    (칸 종류는 보지 않는다 — tasks/0004 단계 3)."""
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
            is_meta = bool(line.get("meta_key")) or line.get("region") == "fields"
            if meta_keys is None:
                if is_meta:
                    skipped["meta_field"] += 1
                    continue
                if line.get("kind") not in kinds:
                    skipped["other_kind"] += 1
                    continue
                if line.get("format") not in (None, "integer"):  # 소수·시각·계기 칸: 정수 숫자 모델의 학습 데이터가 아니다 (tasks/0005)
                    skipped["other_format"] += 1
                    continue
            elif line.get("meta_key") not in meta_keys:
                skipped["other_key"] += 1
                continue
            if "spec" not in line:
                raise CropsError(f"{lf}:{k} 에 크롭 규격(spec)이 없습니다 — 이 버전의 `review export-crops` 로 다시 내보내세요")
            specs.add(CropSpec.from_dict(line["spec"]))
            text = text_of(line) if meta_keys is None else meta_text_of(line)
            if text is None:
                skipped["not_a_number" if meta_keys is None else "no_value"] += 1
                continue
            bbox = tuple(line["bbox"]) if line.get("bbox") else None
            samples.append(Sample(_resolve(root, lf, line["file"]), text, line.get("verdict", "value"),
                                  line.get("work_date"), bbox, line.get("field_id", ""), bool(line.get("inked", True)),
                                  str(line.get("meta_key") or "")))
    if len(specs) > 1:
        raise CropsError(f"크롭 규격이 한 가지가 아닙니다 ({len(specs)}가지: "
                         f"{', '.join(sorted(s.describe() for s in specs))}). 규격마다 따로 내보내고 따로 학습하세요")
    missing = sum(not s.path.is_file() for s in samples)
    if missing:
        raise CropsError(f"labels.jsonl 에 적힌 그림 {missing}개가 없습니다 (폴더를 옮겼거나 일부만 복사했습니다): {root}")
    return Crops(samples, next(iter(specs)) if specs else None, n, dict(skipped), splits)


# ── 검증 날짜 ──────────────────────────────────────────────────────────────
def val_salt(split_salt: str) -> str:
    return f"{split_salt}:val"


def is_val_date(work_date: str | None, salt: str, share: float) -> bool:
    """검증 날짜인가. 날짜가 없는 셀은 검증에 넣지 않는다 (학습 쪽)."""
    return bool(work_date) and split_of(work_date, salt, share) == "test"


def split_val(samples: list[Sample], salt: str, share: float) -> tuple[list[Sample], list[Sample]]:
    """(학습, 검증). 같은 날짜는 한쪽에만 간다."""
    tr, va = [], []
    for s in samples:
        (va if is_val_date(s.work_date, salt, share) else tr).append(s)
    return tr, va


def summarize(samples: list[Sample]) -> dict:
    """카드에 적는 요약: 셀 수, 날짜 수, 판정별·값별 개수 (이미지·field_id·검수자는 적지 않는다)."""
    by_verdict = Counter(s.verdict for s in samples)
    by_value = Counter(s.text for s in samples if s.text not in ("", REJECT))
    return {"cells": len(samples), "dates": len({s.work_date for s in samples if s.work_date}),
            "by_verdict": dict(sorted(by_verdict.items())),
            "by_value": dict(sorted(by_value.items(), key=lambda kv: (len(kv[0]), kv[0])))}
