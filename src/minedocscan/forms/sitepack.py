"""사이트 팩: 한 현장의 양식 정의·마스터·라벨 묶음. 저장소 밖에 둔다 (docs/SITE_PACK.md).

  <site>/
    site.toml                       현장 이름, 파일명 규칙, 장비 분류 매핑, 교차검증 옵션, 평가셋 분할([eval])
    templates/<form>/template.yaml  양식 정의
    templates/<form>/reference.png  기준 이미지 (빈 양식 또는 깨끗한 스캔 한 장)
    labels/pages.json               사람이 붙인 페이지 메타 (날짜·차량번호·작성자) — 인식기가 생기기 전의 대체물
    expected/*.json                 회귀 테스트 기대 수치 (선택)

소프트웨어는 현장을 모른다. 현장에 관한 것은 전부 여기서 읽는다.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from .equipment import META_KEY as EQUIPMENT_KEY
from .equipment import equipment_id, master_keys
from .formats import default_format
from .template import Template, TemplateError


class SitePack:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"사이트 팩 폴더가 없습니다: {self.root}")
        cfg_path = self.root / "site.toml"
        self.config: dict = {}
        if cfg_path.exists():
            try:
                with open(cfg_path, "rb") as f:
                    self.config = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                from ..config import ConfigError

                raise ConfigError(f"site.toml 을 읽을 수 없습니다 ({cfg_path}): {e}") from e
        self.name: str = self.config.get("site", {}).get("name", self.root.name)
        self.templates: dict[str, Template] = {}
        for p in sorted((self.root / "templates").glob("*/template.yaml")):
            t = Template(p)
            if t.name in self.templates:                    # 덮어쓰면 한 양식이 말없이 사라진다
                raise TemplateError(f"템플릿 이름 '{t.name}' 이 둘입니다: {self.templates[t.name].dir.name}/, {t.dir.name}/ — "
                                    "name 은 사이트 팩 안에서 하나여야 합니다")
            self.templates[t.name] = t
        _check_families(self.templates)
        self.equipment_aliases: dict[str, str] = _equipment_aliases(self.config, self.templates)
        self.haul_table: dict[str, list[str]] = _haul_table(self.config)
        self.redact: dict = _redact(self.config)
        if self.redact["meta_keys"] is not None:            # 틀린 키(오타)는 아무것도 가리지 않는다 — 조용히 넘기지 않는다
            known = {f.get("meta_key") for t in self.templates.values() for f in t.fields if f.get("meta_key")}
            unknown = sorted(set(self.redact["meta_keys"]) - known)
            if unknown:
                from ..config import ConfigError

                raise ConfigError(f"site.toml 의 [redact] meta_keys 에 템플릿에 없는 메타 키가 있습니다: {', '.join(unknown)} "
                                  f"(있는 키: {', '.join(sorted(known)) or '없음'})")
        self._labels: dict | None = None
        pat = self.config.get("ingest", {}).get("date_from_filename")
        self._date_re = re.compile(pat) if pat else None

    # ── 평가셋 분할 ────────────────────────────────────────────────────────
    @property
    def split_salt(self) -> str:
        """[eval] split_salt. 없으면 사이트 이름. 바꾸면 평가셋이 바뀐다 (ADR 0009)."""
        return str(self.option("eval", "split_salt", None) or self.name)

    @property
    def test_share(self) -> float:
        return float(self.option("eval", "test_share", 0.2))

    def split_of(self, work_date: str | None) -> str:
        from ..evaluate.split import split_of

        return split_of(work_date, self.split_salt, self.test_share)

    def templates_for(self, day: str | None) -> list[Template]:
        """그날 유효한 템플릿(분류 후보). 날짜를 모르면 전부."""
        return [t for t in self.templates.values() if t.valid_on(day)]

    def concurrent_groups(self, day: str | None) -> dict[str, list[str]]:
        """그날 같이 쓰이는 동시 판의 묶음 (tasks/0006 4.6): {계열: [판 이름 (이름순)]} — 그날 유효한 concurrent 판이 둘 이상인
        계열만. 분류는 묶음을 한 후보로 보고(forms/classify.py), 파이프라인은 판마다 정합해 고른다. 판이 하나뿐인 날은 묶지 않는다."""
        out: dict[str, list[str]] = {}
        for t in self.templates_for(day):
            if t.concurrent and t.family:
                out.setdefault(t.family, []).append(t.name)
        return {fam: sorted(names) for fam, names in sorted(out.items()) if len(names) > 1}

    def variant_families(self) -> dict[str, str]:
        """{판 이름: 계열} — 동시 판(concurrent)만. 리포트가 판마다 고른 쪽을 계열로 묶는 데 쓴다 (report.variant_summary)."""
        return {n: t.family for n, t in self.templates.items() if t.concurrent and t.family}

    def answer_key(self, template_name: str) -> str:
        """정답·검수를 찾는 양식의 키 (tasks/0006 4.6): 동시 판(concurrent)이면 계열, 아니면 템플릿 이름 그대로.
        정답을 템플릿 이름으로 찾는 곳(eval, export-answers 로 만든 정답, oracle)은 이 키로 맞춘다 — 판 A 로 적힌 정답이
        판 B 로 적재된 같은 쪽에도 붙는다. 사이트 팩에 없는 이름은 그대로."""
        t = self.templates.get(template_name)
        return t.family if t is not None and t.concurrent and t.family else template_name

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

    def equipment_id_of(self, name: str | None) -> str | None:
        """적힌 장비명 → 장비 ID. 대응표([equipment.aliases])에 그대로 있는 이름만 — 비슷한 이름으로 맞추지 않는다 (tasks/0005 4.5)."""
        if name is None:
            return None
        key = self.equipment_aliases.get(str(name).strip())
        return None if key is None else equipment_id(key)

    @property
    def equipment_aliases_sha(self) -> str:
        """대응표의 해시: sha256(정렬한 (이름, 장비 키) 쌍의 JSON) 의 앞 16자. `info` 가 이름 대신 낸다 — 대응표를 고쳤는지만 보인다.
        DB 에 남겨 비교하지 않는다: 장비 ID 는 쪽을 적재할 때만 정해지므로 해시가 같아도 ID 가 낡았을 수 있다
        (낡은 행은 `report` 가 센다 — report.stale_equipment_ids)."""
        canon = json.dumps(sorted(self.equipment_aliases.items()), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]

    def known_values(self, key: str) -> list[str]:
        """사이트 팩이 아는 그 키의 값: 템플릿에 인쇄된 값(행렬 머리글의 header_<키> — 나온 만큼) + 장비명이면 대응표의 이름.
        검수 화면의 후보 목록이 쓴다 (나온 횟수 순으로 세고, 라벨·검수에 나온 값은 거기서 더한다)."""
        out = [str(c[f"header_{key}"]) for t in self.templates.values() for reg in t.regions for c in reg["columns"]
               if c.get(f"header_{key}") not in (None, "")]
        if key == EQUIPMENT_KEY:
            out += list(self.equipment_aliases)
        return out

    def option(self, section: str, key: str, default=None):
        cur = self.config
        for part in section.split("."):
            cur = cur.get(part, {})
        return cur.get(key, default)


def _check_families(templates: dict[str, Template]) -> None:
    """같은 family 안에서 유효 기간이 겹치면 오류다. 개정판끼리는 모양으로 가릴 수 없으므로 날짜가 틀림없이 갈라야 한다.
    예외는 같은 날 섞여 쓰이는 판 (tasks/0006 4.6): 겹치는 두 판이 **둘 다** concurrent: true 면 허용한다 — 사람이 적은 것이다.
    그 둘은 괘선 좌표·bbox·기준 그림·인쇄 층·이름·제목·유효 기간 밖의 모든 것이 같아야 한다 (variant_key_diff): field_id 에 템플릿
    이름이 없어 키가 같으면 판이 바뀐 쪽에도 검수가 붙지만, 다르면 조용히 떨어진다.
    concurrent 판이 계열에 하나뿐이어도 오류다: `template variant` 가 새 판에만 적고 기존 판에 적을 두 줄을 안내한 채로 남은 상태 —
    묶음이 생기지 않아 두 판이 따로 분류 후보가 되고, 쪽마다 모양으로 이긴 판 하나에만 정합한다 (판 B 쪽이 판 A 로 적재된다)."""
    by_family: dict[str, list[Template]] = {}
    for t in templates.values():
        if t.family:
            by_family.setdefault(t.family, []).append(t)
    for fam, ts in by_family.items():
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                a0, a1 = a.valid_from or "0000-00-00", a.valid_to or "9999-99-99"
                b0, b1 = b.valid_from or "0000-00-00", b.valid_to or "9999-99-99"
                if not (a0 <= b1 and b0 <= a1):
                    continue
                if not (a.concurrent and b.concurrent):
                    raise TemplateError(f"계열 '{fam}' 의 {a.name} 과 {b.name} 의 유효 기간이 겹칩니다 "
                                        f"({a0}~{a1}, {b0}~{b1}). 옛 판에 valid_to, 새 판에 valid_from 을 적으세요 "
                                        "(같은 날 섞여 쓰이는 판이면 두 판 모두에 concurrent: true)")
                diff = variant_key_diff(a, b)
                if diff:
                    raise TemplateError(f"계열 '{fam}' 의 동시 판 {a.name} 과 {b.name} 의 키가 다릅니다: {diff} — 동시 판끼리는 "
                                        "handler·handler_options, 표(이름·role·header_rows·열·행 — 메타까지), 필드(bbox 밖 전부)가 "
                                        "같아야 합니다 (괘선·bbox·기준 그림·인쇄 층·이름·제목·유효 기간만 다를 수 있다)")
        lone = [t for t in ts if t.concurrent]
        if len(lone) == 1:
            t = lone[0]
            other = templates.get(fam)
            where = (f"{other.name} 의 template.yaml" if other is not None and other is not t
                     else "같은 날 섞여 쓰이는 다른 판의 template.yaml")
            # 그 판이 이미 같은 계열(날짜로 가린 개정판)이면 빠진 것은 concurrent 한 줄이다
            lines = ("concurrent: true" if other is not None and other is not t and other.family == fam
                     else f"family: {fam} 과 concurrent: true")
            raise TemplateError(f"계열 '{fam}' 의 동시 판(concurrent: true)이 {t.name} 하나뿐입니다 — {where} 에 "
                                f"{lines} 를 적습니다 (template variant 가 안내한 줄). "
                                f"섞여 쓰이는 판이 아니면 {t.name} 의 concurrent 를 지웁니다")


# 동시 판끼리 다를 수 있는 것 (tasks/0006 4.6): 기하(괘선·나눔 선, 필드의 bbox), 기준 그림·인쇄 층, 이름·제목, 유효 기간.
# 그 밖은 전부 같아야 한다 — 열·행의 메타(근무조·소계·장소·머리글 …)와 header_rows 가 다르면 같은 field_id 가 다른 뜻의 칸을 가리킨다.
def variant_key_diff(a: Template, b: Template) -> str | None:
    """동시 판 둘이 처음 다른 항목의 종류 (같으면 None). 값(행 키·머리글·메타 값)은 찍지 않는다 — 표 이름과 항목의 종류만."""
    if a.handler != b.handler:
        return "handler"
    if _canon(a.handler_options) != _canon(b.handler_options):
        return "handler_options"
    ra = {str(r.get("name")): r for r in a.regions}
    rb = {str(r.get("name")): r for r in b.regions}
    if sorted(ra) != sorted(rb):
        return "표 이름"
    for name in sorted(ra):
        x, y = ra[name], rb[name]
        if x.get("role") != y.get("role"):
            return f"표 {name} 의 role"
        if (x.get("header_rows") or 0) != (y.get("header_rows") or 0):      # 적지 않으면 0 (Template 과 같은 기본값)
            return f"표 {name} 의 header_rows"
        if _variant_columns(x) != _variant_columns(y):
            return f"표 {name} 의 열"
        if _variant_rows(x) != _variant_rows(y):
            return f"표 {name} 의 행"
        rest = {"grid", "columns", "rows", "role", "header_rows", "name"}
        if _canon({k: v for k, v in x.items() if k not in rest}) != _canon({k: v for k, v in y.items() if k not in rest}):
            return f"표 {name} 의 그 밖의 항목"
    if _variant_fields(a) != _variant_fields(b):
        return "필드"
    if a.spec.get("display") != b.spec.get("display"):          # 엑셀의 시트는 계열마다 하나다 (tasks/0008 4.5)
        return "양식의 display"
    return None


def _canon(v) -> str:
    """비교용 정규형: 키 순서와 무관하게 (YAML 의 날짜·숫자는 문자열로)."""
    return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)


def _with_format(d: dict) -> dict:
    """형식을 적지 않은 칸은 칸 종류의 기본 형식으로 본다 — 실제로 쓰는 형식을 비교한다."""
    return {**d, "format": d.get("format") or default_format(d.get("kind", ""))}


def _variant_columns(reg: dict) -> list[str]:
    return sorted(_canon(_with_format(c)) for c in reg.get("columns") or [])


def _variant_rows(reg: dict) -> list[str]:
    return sorted(_canon(r) for r in reg.get("rows") or [])


def _variant_fields(t: Template) -> list[str]:
    return sorted(_canon({k: v for k, v in _with_format(f).items() if k != "bbox"}) for f in t.fields)


def _haul_table(config: dict) -> dict[str, list[str]]:
    """site.toml 의 [haul_table] (월별 엑셀의 운반 표 — tasks/0008 4.4): columns = ["광종|편", …] 열의 순서, slots = ["T01", …]
    자리의 순서. 거기에 없는 것은 뒤에 붙는다 (빠뜨리지 않는다). 현장의 것이라 코드에 적지 않는다. 틀리면 ConfigError 한 줄
    (값은 찍지 않는다 — 몇 번째 항목인지만)."""
    from ..config import ConfigError

    raw = config.get("haul_table") or {}
    if not isinstance(raw, dict):
        raise ConfigError("site.toml 의 [haul_table] 은 표여야 합니다 (columns = [...], slots = [...])")
    out: dict[str, list[str]] = {}
    for key in ("columns", "slots"):
        v = raw.get(key, [])
        if not isinstance(v, list):
            raise ConfigError(f"site.toml 의 [haul_table] {key} 는 글자의 목록이어야 합니다")
        for i, x in enumerate(v, 1):
            if not isinstance(x, str) or not x.strip() or (key == "columns" and x.count("|") != 1):
                raise ConfigError(f"site.toml 의 [haul_table] {key} {i}번째 항목: "
                                  + ("\"광종|편\" 꼴의 글자여야 합니다" if key == "columns" else "빈 문자열이 아닌 글자여야 합니다"))
        if len(set(v)) != len(v):
            raise ConfigError(f"site.toml 의 [haul_table] {key} 에 겹치는 항목이 있습니다")
        out[key] = [x.strip() for x in v]
    unknown = sorted(set(raw) - {"columns", "slots"})
    if unknown:
        raise ConfigError(f"site.toml 의 [haul_table] 에 모르는 키가 있습니다: {', '.join(unknown)} (columns, slots)")
    return out


def _redact(config: dict) -> dict:
    """site.toml 의 [redact] (가린 쪽 그림 — tasks/0008 4.9): meta_keys = ["operator", …] 가릴 메타 키 (없으면 None — 기본은
    export/masked.DEFAULT_META_KEYS), pad_px = 표 밖 필드·redact 상자를 넓히는 폭 (없으면 None — 기본은 export/masked.DEFAULT_PAD_PX).
    현장의 것이라 코드에 적지 않는다. 틀리면 ConfigError 한 줄."""
    from ..config import ConfigError

    raw = config.get("redact") or {}
    if not isinstance(raw, dict):
        raise ConfigError("site.toml 의 [redact] 는 표여야 합니다 (meta_keys = [...], pad_px = N)")
    unknown = sorted(set(raw) - {"meta_keys", "pad_px"})
    if unknown:
        raise ConfigError(f"site.toml 의 [redact] 에 모르는 키가 있습니다: {', '.join(unknown)} (meta_keys, pad_px)")
    keys = raw.get("meta_keys")
    if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) and k.strip() for k in keys)):
        raise ConfigError("site.toml 의 [redact] meta_keys 는 메타 키(글자)의 목록이어야 합니다 — 예: [\"operator\", \"vehicle_no\"]")
    pad = raw.get("pad_px")
    if pad is not None and (not isinstance(pad, int) or isinstance(pad, bool) or not 0 <= pad <= 200):
        raise ConfigError(f"site.toml 의 [redact] pad_px 는 0–200 의 정수여야 합니다: {pad!r}")
    return {"meta_keys": [k.strip() for k in keys] if keys is not None else None, "pad_px": pad}


def _equipment_aliases(config: dict, templates: dict[str, Template]) -> dict[str, str]:
    """site.toml 의 [equipment.aliases]: 적힌 이름 → 장비 키. 키는 마스터(점검표 템플릿의 장비 행)에 있어야 한다 — 없으면 오류.
    오류 메시지에 이름·장비 키를 찍지 않는다 (몇 번째 항목인지만)."""
    from ..config import ConfigError

    raw = (config.get("equipment") or {}).get("aliases") or {}
    if not isinstance(raw, dict):
        raise ConfigError("site.toml 의 [equipment.aliases] 는 표(이름 = \"장비 키\")여야 합니다")
    master = master_keys(templates.values())
    out: dict[str, str] = {}
    for i, (name, key) in enumerate(raw.items(), 1):
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"site.toml 의 [equipment.aliases] {i}번째 항목: 장비 키는 문자열이어야 합니다")
        if key not in master:
            raise ConfigError(f"site.toml 의 [equipment.aliases] {i}번째 항목이 마스터에 없는 장비 키를 가리킵니다 "
                              f"(마스터 = 점검표 템플릿의 장비 행 {len(master)}개)")
        out[str(name).strip()] = key
    return out
