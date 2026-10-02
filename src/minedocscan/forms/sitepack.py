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

from .template import Template


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
        self._labels: dict | None = None
        pat = self.config.get("ingest", {}).get("date_from_filename")
        self._date_re = re.compile(pat) if pat else None

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
