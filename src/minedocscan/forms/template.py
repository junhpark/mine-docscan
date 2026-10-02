"""템플릿: 양식 하나의 정의. 양식은 코드가 아니라 사이트 팩의 templates/<name>/template.yaml 이다.

형식은 docs/SITE_PACK.md 에 있다. 요약:

  name, title, reference_image, dpi, page_size
  handler: generic | inspection | haul        # 추출값을 업무 테이블로 옮기는 방법
  handler_options: {...}
  regions:                                    # 한 페이지에 표가 여러 개일 수 있다
    - name, grid: {ys, xs}, header_rows
      columns: [{idx, name, kind, ...메타}]   # kind: printed | handwritten_text | handwritten_number | checkmark
      rows:    [{row, key, ...메타}]
  fields: [{name, kind, bbox}]                # 표 밖의 자유 필드 (날짜, 작성자, 비고 …)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from ..imaging.align import orb_features
from ..imaging.io import imread_gray

CELL_KINDS = {"printed", "handwritten_text", "handwritten_number", "checkmark", "signature"}


@dataclass
class Cell:
    region: str
    row: int                 # 데이터 행 번호(헤더 제외, 0부터). 자유 필드는 -1
    col: int
    name: str                # 컬럼 이름 또는 필드 이름
    kind: str
    bbox: tuple[int, int, int, int]      # 템플릿 좌표계 x0,y0,x1,y1
    row_key: str = ""        # 행을 식별하는 키 (장비, 광종|편 …)
    col_meta: dict = field(default_factory=dict)
    row_meta: dict = field(default_factory=dict)


class TemplateError(ValueError):
    pass


def row_key(row: dict) -> str:
    """행 키. 키가 없는 행(양식의 여백 행)은 '#<행 번호>' 로 구분해, 한 표 안에서 키가 겹치지 않게 한다."""
    k = str(row.get("key", "") or "")
    return k or f"#{row['row']}"


class Template:
    def __init__(self, path: str | Path):
        path = Path(path)
        self.path = path
        self.dir = path.parent
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        self.spec = spec
        self.name: str = spec["name"]
        self.title: str = spec.get("title", self.name)
        self.handler: str = spec.get("handler", "generic")
        self.handler_options: dict = spec.get("handler_options", {}) or {}
        self.regions: list[dict] = spec.get("regions", []) or []
        self.fields: list[dict] = spec.get("fields", []) or []
        self._ref: np.ndarray | None = None
        self._feats = None
        self._validate()

    def _validate(self) -> None:
        for reg in self.regions:
            ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
            if ys != sorted(ys) or xs != sorted(xs):
                raise TemplateError(f"{self.name}/{reg['name']}: 괘선 좌표는 오름차순이어야 합니다")
            for c in reg["columns"]:
                if c["kind"] not in CELL_KINDS:
                    raise TemplateError(f"{self.name}/{reg['name']}: 알 수 없는 kind '{c['kind']}'")
                if not 0 <= c["idx"] < len(xs) - 1:
                    raise TemplateError(f"{self.name}/{reg['name']}: 컬럼 idx {c['idx']} 가 괘선 범위를 벗어납니다")
            hr = reg.get("header_rows", 0)
            keys = set()
            for r in reg["rows"]:
                if r["row"] + hr + 1 >= len(ys):
                    raise TemplateError(f"{self.name}/{reg['name']}: 행 {r['row']} 가 괘선 범위를 벗어납니다")
                if row_key(r) in keys:
                    raise TemplateError(f"{self.name}/{reg['name']}: 행 키 '{row_key(r)}' 가 겹칩니다")
                keys.add(row_key(r))

    @property
    def has_cells(self) -> bool:
        """표 정의가 있는가. 없으면 분류용 기준 이미지만 있는 스텁이다."""
        return bool(self.regions)

    @property
    def reference(self) -> np.ndarray:
        if self._ref is None:
            self._ref = imread_gray(self.dir / self.spec["reference_image"])
        return self._ref

    @property
    def features(self):
        """기준 이미지의 ORB 특징점 (정합용, 한 번만 계산)."""
        if self._feats is None:
            self._feats = orb_features(self.reference)
        return self._feats

    def region(self, name: str) -> dict:
        for reg in self.regions:
            if reg["name"] == name:
                return reg
        raise KeyError(name)

    def cells(self, inset: int = 4) -> list[Cell]:
        """모든 표의 데이터 셀. inset 은 괘선을 피하기 위한 안쪽 여백(px)."""
        out = []
        for reg in self.regions:
            ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
            hr = reg.get("header_rows", 0)
            for r in reg["rows"]:
                gi = r["row"] + hr
                y0, y1 = ys[gi], ys[gi + 1]
                for c in reg["columns"]:
                    x0, x1 = xs[c["idx"]], xs[c["idx"] + 1]
                    meta = {k: v for k, v in c.items() if k not in ("idx", "name", "kind")}
                    out.append(Cell(reg["name"], r["row"], c["idx"], c["name"], c["kind"],
                                    (x0 + inset, y0 + inset, x1 - inset, y1 - inset), row_key(r), meta, r))
        return out

    def field_cells(self) -> list[Cell]:
        return [Cell("fields", -1, -1, f["name"], f["kind"], tuple(f["bbox"])) for f in self.fields]
