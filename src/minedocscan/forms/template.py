"""템플릿: 양식 하나의 정의. 양식은 코드가 아니라 사이트 팩의 templates/<name>/template.yaml 이다.

형식은 docs/SITE_PACK.md 에 있다. 요약:

  name, title, reference_image, dpi, page_size
  family, valid_from, valid_to                # 선택. 같은 양식의 개정판은 이름이 다른 템플릿이고, 날짜(양 끝 포함)로 가린다
  handler: generic | inspection | haul | usage   # 추출값을 업무 테이블로 옮기는 방법
  handler_options: {...}
  regions:                                    # 한 페이지에 표가 여러 개일 수 있다
    - name, grid: {ys, xs, split_ys?, split_xs?}, header_rows, role?
      columns: [{idx, name, kind, format?, ...메타}]   # kind: printed | handwritten_text | handwritten_number | checkmark
      rows:    [{row, key, ...메타}]
                                              # split_ys/split_xs: 인쇄되지 않은 나눔 선 — 한 칸 안의 인쇄된 줄(하단 / 저광장)마다
                                              #   행·열을 나눌 때. 칸을 자르는 데만 쓰고 정합 판정·괘선 지우기에는 쓰지 않는다
                                              #   (없는 괘선을 찾으면 정합이 실패한다). row·idx 는 괘선과 나눔 선을 합친 순서다
                                              # role: usage 핸들러의 표의 역할 — meter | shifts | tally | activities (tasks/0005 4.2)
                                              #   meter: 열 이름(또는 행 키) start·end·total, tally: 소계 칸은 열·행 메타 subtotal: true
  fields: [{name, kind, bbox, meta_key?, format?}]     # 표 밖의 자유 필드 (날짜, 작성자, 비고 …)
                                              # format: 손으로 쓰는 칸의 값의 형식 — integer | decimal | time | time_range | reading
                                              #   (forms/formats.py, tasks/0005 4.1). 숫자 칸의 기본은 integer, 글자 칸은 없음
                                              # meta_key: 이 필드의 값이 쪽의 메타(vehicle_no, operator …)가 된다. date 는 안 된다
                                              # date.month, date.day: 읽기 전용 — 날짜의 월·일을 적는 칸. 값은 쪽의 날짜(파일명·라벨)에서
                                              #   오고 기계가 읽은 값은 대조에만 쓴다. 검수로 받지 않는다 (tasks/0004 4.3)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import yaml

from ..imaging.align import orb_features
from ..imaging.io import imread_gray
from .formats import FORMAT_KINDS, FORMATS, default_format

CELL_KINDS = {"printed", "handwritten_text", "handwritten_number", "checkmark", "signature"}
DATE_PARTS = ("date.month", "date.day")      # 읽기 전용 메타 키: 정답은 쪽의 날짜에서 나온다. 검수로 받지 않는다
ROLES = ("meter", "shifts", "tally", "activities")      # usage 핸들러의 표의 역할 (tasks/0005 4.2)
METER_SLOTS = ("start", "end", "total")                 # 계기 표의 칸: 열 이름 또는 행 키
METER_REQUIRED = ("start", "end")                       # 총(total)은 없어도 된다 — 종료 − 시작이 먼저다
METER_FORMATS = ("reading", "decimal", "time")          # 계기 칸의 형식: 계기 값 또는 시각


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

    @property
    def fmt(self) -> str | None:
        """값의 형식 (forms/formats.py): 템플릿의 format, 없으면 칸 종류의 기본 (숫자 칸 integer, 글자 칸 없음)."""
        return self.col_meta.get("format") or default_format(self.kind)


class TemplateError(ValueError):
    pass


def row_key(row: dict) -> str:
    """행 키. 키가 없는 행(양식의 여백 행)은 '#<행 번호>' 로 구분해, 한 표 안에서 키가 겹치지 않게 한다."""
    k = str(row.get("key", "") or "")
    return k or f"#{row['row']}"


def _iso_date(name: str, key: str, v) -> str | None:
    if v is None or v == "":
        return None
    try:
        return date.fromisoformat(str(v)).isoformat()         # YAML 이 date 로 읽어도, 문자열이어도
    except ValueError as e:
        raise TemplateError(f"{name}: {key} 는 YYYY-MM-DD 여야 합니다: {v!r}") from e


class Template:
    def __init__(self, path: str | Path, validate: bool = True):
        """validate=False: 오류가 있어도 읽기만 한다 (template check 가 problems() 로 전부 모은다)."""
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
        self.family: str | None = spec.get("family") or None
        self.valid_from: str | None = _iso_date(self.name, "valid_from", spec.get("valid_from"))
        self.valid_to: str | None = _iso_date(self.name, "valid_to", spec.get("valid_to"))
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise TemplateError(f"{self.name}: valid_from 이 valid_to 보다 늦습니다")
        self._ref: np.ndarray | None = None
        self._feats = None
        if validate:
            self._validate()

    def _validate(self) -> None:
        errs = self.problems()
        if errs:
            raise TemplateError(errs[0])

    def problems(self) -> list[str]:
        """읽을 때의 오류 전부 (첫째가 TemplateError 가 된다). `template check` 는 여기에 기하 검사(겹침·쪽 밖)를 더한다."""
        out: list[str] = []
        names = set()
        for f in self.fields:
            where = f"{self.name}/fields/{f.get('name')}"
            if f.get("name") in names:
                out.append(f"{self.name}: 자유 필드 이름 '{f.get('name')}' 이 겹칩니다")
            names.add(f.get("name"))
            if f.get("kind") not in CELL_KINDS:
                out.append(f"{where}: 알 수 없는 kind '{f.get('kind')}'")
            mk = f.get("meta_key")
            if mk is not None and (not isinstance(mk, str) or not mk):
                out.append(f"{where}: meta_key 는 빈 문자열이 아니어야 합니다")
            if mk == "date":
                out.append(f"{where}: meta_key 'date' 는 받지 않습니다 — 날짜는 파일명 규칙과 라벨로 정한다 "
                           "(월·일 칸은 date.month, date.day — 대조에만 쓴다)")
            if isinstance(mk, str) and mk.startswith("date.") and mk not in DATE_PARTS:
                out.append(f"{where}: 날짜의 부분은 {' | '.join(DATE_PARTS)} 만 받습니다: {mk!r}")
            out += _format_problems(where, f)
        mks = [f["meta_key"] for f in self.fields if f.get("meta_key")]
        dup = sorted({k for k in mks if mks.count(k) > 1})
        if dup:
            out.append(f"{self.name}: 같은 meta_key 를 가진 필드가 둘 이상입니다: {dup}")
        for reg in self.regions:
            out += self._region_problems(reg)
        if self.handler == "usage":
            for role in ("meter", "shifts"):                # 가동 기록은 쪽 하나에 한 행 — 계기·근무 시각 표는 하나씩만
                n = sum(reg.get("role") == role for reg in self.regions)
                if n > 1:
                    out.append(f"{self.name}: role {role} 인 표가 {n}개입니다 (쪽 하나에 하나)")
        return out

    def _region_problems(self, reg: dict) -> list[str]:
        out: list[str] = []
        where = f"{self.name}/{reg.get('name')}"
        try:
            ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
            sys_, sxs = reg["grid"].get("split_ys") or [], reg["grid"].get("split_xs") or []
            columns, rows = reg["columns"], reg["rows"]
        except (KeyError, TypeError) as e:
            return [f"{where}: 표에 {e} 가 없습니다 (grid.ys, grid.xs, columns, rows)"]
        if ys != sorted(ys) or xs != sorted(xs):
            out.append(f"{where}: 괘선 좌표는 오름차순이어야 합니다")
        for axis, split, lines in (("split_ys", sys_, ys), ("split_xs", sxs, xs)):
            if split != sorted(split) or any(not (min(lines) < v < max(lines)) or v in lines for v in split):
                out.append(f"{where}: {axis} 는 괘선 사이의 오름차순 좌표여야 합니다 (괘선과 같은 값은 안 된다)")
        all_ys, all_xs = sorted(set(ys) | set(sys_)), sorted(set(xs) | set(sxs))
        role = reg.get("role")
        if role is not None and role not in ROLES:
            out.append(f"{where}: 알 수 없는 role {role!r} (가능: {', '.join(ROLES)})")
        if role is not None and self.handler != "usage":
            out.append(f"{where}: role 은 usage 핸들러의 표에만 씁니다 (handler 가 {self.handler!r})")
        for c in columns:
            if c.get("kind") not in CELL_KINDS:
                out.append(f"{where}: 알 수 없는 kind '{c.get('kind')}'")
            if not isinstance(c.get("idx"), int) or not 0 <= c["idx"] < len(all_xs) - 1:
                out.append(f"{where}: 컬럼 idx {c.get('idx')} 가 괘선 범위를 벗어납니다")
            out += _format_problems(f"{where}/{c.get('name')}", c)
        hr = reg.get("header_rows", 0)
        keys = set()
        for r in rows:
            if not isinstance(r.get("row"), int) or r["row"] + hr + 1 >= len(all_ys):
                out.append(f"{where}: 행 {r.get('row')} 가 괘선 범위를 벗어납니다")
            if row_key(r) in keys:                              # 키 값은 찍지 않는다 (점검표의 행 키는 장비 번호다)
                out.append(f"{where}: 행 {r.get('row')} 의 행 키가 앞의 행과 겹칩니다")
            keys.add(row_key(r))
        for x in [*columns, *rows]:
            st = x.get("subtotal")
            if st is not None and not isinstance(st, bool):
                out.append(f"{where}: subtotal 은 true/false: {st!r}")
            # 소계: 작업량 표의 열·행 (검산 대상), 작업 표의 합계 줄 (글씨가 있는 줄의 수에서 뺀다)
            if st and not (role == "tally" or (role == "activities" and x in rows)):
                out.append(f"{where}: subtotal 은 role tally 인 표의 열·행, role activities 인 표의 행에만 씁니다")
        out += _role_problems(where, role, columns, rows)
        return out

    def valid_on(self, day: str | None) -> bool:
        """그날 쓰이는 판인가. 유효 기간이 없으면 언제나, 날짜를 모르면 언제나 후보다."""
        if day is None:
            return True
        return (self.valid_from is None or self.valid_from <= day) and (self.valid_to is None or day <= self.valid_to)

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
        return [c for reg in self.regions for c in region_cells(reg, inset)]

    def field_cells(self) -> list[Cell]:
        return [Cell("fields", -1, -1, f["name"], f["kind"], tuple(f["bbox"]),
                     col_meta={k: f[k] for k in ("meta_key", "format") if f.get(k)}) for f in self.fields]

    def format_of(self, region: str, name: str) -> str | None:
        """칸(표의 열 이름 또는 자유 필드 이름)의 값의 형식. 없는 칸이면 None (형식 검사를 하지 않는다)."""
        if region == "fields":
            for f in self.fields:
                if f["name"] == name:
                    return f.get("format") or default_format(f.get("kind", ""))
            return None
        for reg in self.regions:
            if reg["name"] == region:
                for c in reg["columns"]:
                    if c["name"] == name:
                        return c.get("format") or default_format(c["kind"])
        return None

    def meta_fields(self) -> dict[str, str]:
        """쪽의 메타를 적는 자유 필드: {필드 이름: meta_key}. 날짜의 부분(date.month, date.day)도 들어 있다."""
        return {f["name"]: f["meta_key"] for f in self.fields if f.get("meta_key")}

    def review_meta_fields(self) -> dict[str, str]:
        """검수값이 쪽의 메타가 되는 자유 필드 — meta_fields 에서 날짜의 부분(읽기 전용)을 뺀 것."""
        return {n: k for n, k in self.meta_fields().items() if k not in DATE_PARTS}


def region_cells(reg: dict, inset: int = 4) -> list[Cell]:
    """표 하나의 데이터 셀 (Template.cells 의 한 표 몫). 행·열이 괘선 범위를 벗어나면 IndexError 등 — template check 가 표마다 잡는다."""
    out = []
    ys, xs = cell_lines(reg)
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


def cell_lines(reg: dict) -> tuple[list[int], list[int]]:
    """칸을 자르는 선: 괘선 + 인쇄되지 않은 나눔 선 (split_ys, split_xs). 정합·괘선 지우기는 괘선(grid.ys, grid.xs)만 쓴다."""
    g = reg["grid"]
    return sorted(set(g["ys"]) | set(g.get("split_ys") or [])), sorted(set(g["xs"]) | set(g.get("split_xs") or []))


def _format_problems(where: str, spec: dict) -> list[str]:
    """칸·필드의 format: 알려진 형식이고, 손으로 쓰는 칸에만 (인쇄된 칸·체크·서명에는 형식이 없다)."""
    fmt = spec.get("format")
    if fmt is None:
        return []
    if fmt not in FORMATS:
        return [f"{where}: 알 수 없는 format {fmt!r} (가능: {', '.join(FORMATS)})"]
    if spec.get("kind") not in FORMAT_KINDS:
        return [f"{where}: format 은 손으로 쓰는 칸({' | '.join(FORMAT_KINDS)})에만 — kind 가 {spec.get('kind')!r}"]
    return []


def meter_slot(name: str, row_key: str | None) -> str | None:
    """계기 표의 칸(열 이름, 행 키)이 시작·종료·총 중 무엇인가: 열 이름, 아니면 행 키 (세로로 놓인 계기 칸)."""
    if name in METER_SLOTS:
        return name
    return row_key if row_key in METER_SLOTS else None


def _role_problems(where: str, role: str | None, columns: list[dict], rows: list[dict]) -> list[str]:
    """표의 역할에 필요한 칸 (tasks/0005 4.2)."""
    hand = [c for c in columns if str(c.get("kind", "")).startswith("handwritten")]
    fmt = lambda c: c.get("format") or default_format(c.get("kind", ""))      # noqa: E731
    if role == "meter":
        slots = {c["name"]: c for c in hand if c.get("name") in METER_SLOTS}
        by_row = {row_key(r) for r in rows if row_key(r) in METER_SLOTS}
        out = []
        if slots:                                            # 가로: 열 이름이 start·end·total
            missing = [s for s in METER_REQUIRED if s not in slots]
            bad = [n for n, c in slots.items() if fmt(c) not in METER_FORMATS]
        else:                                                # 세로: 행 키가 start·end·total, 손으로 쓰는 열 하나
            missing = [s for s in METER_REQUIRED if s not in by_row]
            bad = [c["name"] for c in hand if fmt(c) not in METER_FORMATS]
            if len(hand) != 1:
                out.append(f"{where}: role meter 의 세로 표는 손으로 쓰는 열이 하나여야 합니다 ({len(hand)}개)")
        if missing:
            out.append(f"{where}: role meter 에 필요한 칸이 없습니다: {', '.join(missing)} (열 이름 또는 행 키 start·end·total)")
        if bad:
            out.append(f"{where}: role meter 의 칸 형식은 {' | '.join(METER_FORMATS)}: {', '.join(bad)}")
        return out
    if role == "shifts" and not any(fmt(c) == "time_range" for c in hand):
        return [f"{where}: role shifts 에는 format time_range 인 열이 있어야 합니다"]
    if role == "tally":
        ints = [c for c in hand if fmt(c) == "integer"]
        if not ints:
            return [f"{where}: role tally 에는 정수(integer) 칸이 있어야 합니다"]
        bad = [c["name"] for c in columns if c.get("subtotal") and c not in ints]
        if bad:
            return [f"{where}: subtotal 열은 정수 칸이어야 합니다: {', '.join(bad)}"]
    return []
