"""사이트 팩: 한 현장의 양식 정의·마스터·라벨 묶음. 저장소 밖에 둔다 (docs/SITE_PACK.md).

  <site>/
    site.toml                       현장 이름, 파일명 규칙, 장비 분류 매핑, 교차검증 옵션
    templates/<form>/template.yaml  양식 정의
    templates/<form>/reference.png  기준 이미지 (빈 양식 또는 깨끗한 스캔 한 장)
    labels/pages.json               사람이 붙인 페이지 메타 (날짜·차량번호·작성자) — 인식기가 생기기 전의 대체물
    expected/*.json                 회귀 테스트 기대 수치 (선택)

소프트웨어는 현장을 모른다. 현장에 관한 것은 전부 여기서 읽는다.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from .template import Template, TemplateError


class SitePack:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"사이트 팩 폴더가 없습니다: {self.root}")
        cfg_path = self.root / "site.toml"
        self.config: dict = {}
        if cfg_path.exists():
            with open(cfg_path, "rb") as f:
                self.config = tomllib.load(f)
        self.name: str = self.config.get("site", {}).get("name", self.root.name)
        self.templates: dict[str, Template] = {}
        for p in sorted((self.root / "templates").glob("*/template.yaml")):
            t = Template(p)
            self.templates[t.name] = t
        _check_families(self.templates)
        self._labels: dict | None = None
        pat = self.config.get("ingest", {}).get("date_from_filename")
        self._date_re = re.compile(pat) if pat else None

    def templates_for(self, day: str | None) -> list[Template]:
        """그날 유효한 템플릿(분류 후보). 날짜를 모르면 전부."""
        return [t for t in self.templates.values() if t.valid_on(day)]

    # ── 페이지 메타 ────────────────────────────────────────────────────────
    @property
    def labels(self) -> dict:
        if self._labels is None:
            p = self.root / "labels" / "pages.json"
            self._labels = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        return self._labels

    def page_meta(self, source_stem: str, page_no: int) -> dict:
        """페이지의 날짜·차량번호·작성자. 우선순위: 페이지 라벨 > 문서 라벨 > 파일명 규칙."""
        meta: dict = {}
        d = self.date_from_filename(source_stem)
        if d:
            meta["date"] = d
        meta.update(self.labels.get(source_stem, {}))
        meta.update(self.labels.get(f"{source_stem}#{page_no}", {}))
        return meta

    def date_from_filename(self, stem: str) -> str | None:
        """site.toml 의 [ingest] date_from_filename 정규식(이름 있는 그룹 yy|yyyy, mm, dd)으로 날짜를 뽑는다."""
        if not self._date_re:
            return None
        m = self._date_re.search(stem)
        if not m:
            return None
        g = m.groupdict()
        year = g.get("yyyy") or ("20" + g["yy"])
        return f"{year}-{g['mm']}-{g['dd']}"

    # ── 현장 옵션 ──────────────────────────────────────────────────────────
    def iso_type(self, site_category: str) -> str | None:
        """현장의 장비 구분 → ISO 23725 Table 11 장비 유형. 대응이 없으면 None."""
        return self.config.get("equipment", {}).get("iso_type", {}).get(site_category) or None

    def option(self, section: str, key: str, default=None):
        cur = self.config
        for part in section.split("."):
            cur = cur.get(part, {})
        return cur.get(key, default)


def _check_families(templates: dict[str, Template]) -> None:
    """같은 family 안에서 유효 기간이 겹치면 오류다. 개정판끼리는 모양으로 가릴 수 없으므로 날짜가 틀림없이 갈라야 한다."""
    by_family: dict[str, list[Template]] = {}
    for t in templates.values():
        if t.family:
            by_family.setdefault(t.family, []).append(t)
    for fam, ts in by_family.items():
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                a0, a1 = a.valid_from or "0000-00-00", a.valid_to or "9999-99-99"
                b0, b1 = b.valid_from or "0000-00-00", b.valid_to or "9999-99-99"
                if a0 <= b1 and b0 <= a1:
                    raise TemplateError(f"계열 '{fam}' 의 {a.name} 과 {b.name} 의 유효 기간이 겹칩니다 "
                                        f"({a0}~{a1}, {b0}~{b1}). 옛 판에 valid_to, 새 판에 valid_from 을 적으세요")
