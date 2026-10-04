"""실행 설정. 경로는 코드에 적지 않고 설정 파일과 환경변수로만 받는다.

우선순위: 환경변수 > 설정 파일(TOML) > 기본값.

  MINEDOCSCAN_CONFIG        설정 파일 경로 (기본: ./minedocscan.toml)
  MINEDOCSCAN_ARCHIVE_ROOT  스캔 원본 보관 폴더 (읽기 전용으로 취급)
  MINEDOCSCAN_WORK_ROOT     작업 폴더 — 정합 이미지, DB, 리포트 (로컬 디스크 권장)
  MINEDOCSCAN_SITE          사이트 팩 폴더 (템플릿·마스터·라벨)
  MINEDOCSCAN_DB_URL        DB 주소 (기본: sqlite:///<work_root>/minedocscan.db)
  MINEDOCSCAN_REVIEWS       검수 기록 파일 (기본: <site>/reviews/reviews.jsonl — 사이트 팩 안, 추가 전용)
  MINEDOCSCAN_DAMAGED_PDF   손상 PDF(라이브러리가 복구해서 연 파일)의 처리: fail(기본) | warn

값이 틀리면(TOML 문법, 숫자가 아닌 숫자 값, 범위 밖, 모르는 선택지) ConfigError 하나로 무엇이 틀렸는지 한 줄로 알린다.
명령줄(cli.main)은 그것을 트레이스백 없이 보여 주고 0 이 아닌 코드로 끝난다.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DAMAGED_PDF = ("fail", "warn")


class ConfigError(ValueError):
    """설정 값이 틀렸다. 메시지 한 줄에 어느 항목이 왜 틀렸는지 적는다 (트레이스백 없이 보여 준다)."""


@dataclass
class Settings:
    archive_root: Path | None = None
    work_root: Path = Path("work")
    site: Path | None = None
    db_url: str | None = None
    reviews: Path | None = None          # 검수 기록(jsonl). None 이면 사이트 팩의 reviews/reviews.jsonl
    dpi: int = 200                       # 템플릿 좌표계의 해상도. 템플릿을 만든 해상도와 같아야 한다
    recognizer: str = "null"             # recognize.REGISTRY 의 이름 — 기본 백엔드
    recognizer_by_kind: dict = field(default_factory=dict)   # [recognize.by_kind] 칸 종류 → 백엔드 이름
    recognizer_options: dict = field(default_factory=dict)   # [recognize.<백엔드>] 표 — 예: {"digits": {"model": "digits-v1"}}
    corrector: str = "none"              # correct.REGISTRY 의 이름
    auto_accept_conf: float = 0.90       # 이 신뢰도 이상이면 검수 없이 적재
    classify_min_margin: float = 1.5     # 양식 분류 1위/2위 비율이 이보다 낮으면 검수 표시
    save_aligned: bool = True
    source_dpi: int = 300                # 원본 해상도 크롭을 뜰 때 PDF 를 렌더링하는 해상도 (스캔 원본이 300 dpi)
    damaged_pdf: str = "fail"            # 라이브러리가 복구해서 연 PDF: fail(문서 실패) | warn(처리하고 경고를 남김)
    extra: dict = field(default_factory=dict)

    @property
    def aligned_dir(self) -> Path:
        return self.work_root / "aligned"

    @property
    def reports_dir(self) -> Path:
        return self.work_root / "reports"

    @property
    def resolved_db_url(self) -> str:
        return self.db_url or f"sqlite:///{(self.work_root / 'minedocscan.db').as_posix()}"

    def reviews_path(self, site_root: Path | None = None) -> Path:
        """검수 기록 파일. 설정이 없으면 사이트 팩 안이다 — WORK_ROOT 와 달리 지워지지 않는 곳."""
        if self.reviews is not None:
            return self.reviews
        root = site_root or self.site
        if root is None:
            raise ValueError("검수 파일 경로를 정할 수 없습니다: [paths] reviews 또는 MINEDOCSCAN_REVIEWS, 아니면 사이트 팩")
        return Path(root) / "reviews" / "reviews.jsonl"


def load_settings(config_path: str | os.PathLike | None = None, **overrides) -> Settings:
    path = Path(config_path or os.environ.get("MINEDOCSCAN_CONFIG", "minedocscan.toml"))
    raw: dict = {}
    if path.exists():
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"설정 파일을 읽을 수 없습니다 ({path}): {e}") from e
    paths = _table(raw, "paths", path)
    pipe = _table(raw, "pipeline", path)
    rec = _table(raw, "recognize", path)
    by_kind = rec.get("by_kind", {}) or {}
    if not isinstance(by_kind, dict):
        raise ConfigError(f"[recognize.by_kind] 는 표여야 합니다 ({path})")
    s = Settings(
        archive_root=_p(paths.get("archive_root")),
        work_root=_p(paths.get("work_root")) or Path("work"),
        site=_p(paths.get("site")),
        db_url=_table(raw, "database", path).get("url"),
        reviews=_p(paths.get("reviews")),
        dpi=_number(pipe, "dpi", 200, int, "[pipeline] dpi", lo=50, hi=1200),
        recognizer=str(rec.get("backend", "null")),
        recognizer_by_kind=dict(by_kind),
        recognizer_options={k: dict(v) for k, v in rec.items() if isinstance(v, dict) and k != "by_kind"},
        corrector=str(_table(raw, "correct", path).get("backend", "none")),
        auto_accept_conf=_number(pipe, "auto_accept_conf", 0.90, float, "[pipeline] auto_accept_conf", lo=0.0, hi=1.0),
        classify_min_margin=_number(pipe, "classify_min_margin", 1.5, float, "[pipeline] classify_min_margin", lo=0.0),
        save_aligned=_flag(pipe, "save_aligned", True, "[pipeline] save_aligned"),
        source_dpi=_number(_table(raw, "review", path), "source_dpi", 300, int, "[review] source_dpi", lo=50, hi=1200),
        damaged_pdf=str(pipe.get("damaged_pdf", "fail")),
        extra=raw,
    )
    env = os.environ
    if env.get("MINEDOCSCAN_ARCHIVE_ROOT"):
        s.archive_root = Path(env["MINEDOCSCAN_ARCHIVE_ROOT"])
    if env.get("MINEDOCSCAN_WORK_ROOT"):
        s.work_root = Path(env["MINEDOCSCAN_WORK_ROOT"])
    if env.get("MINEDOCSCAN_SITE"):
        s.site = Path(env["MINEDOCSCAN_SITE"])
    if env.get("MINEDOCSCAN_DB_URL"):
        s.db_url = env["MINEDOCSCAN_DB_URL"]
    if env.get("MINEDOCSCAN_REVIEWS"):
        s.reviews = Path(env["MINEDOCSCAN_REVIEWS"])
    if env.get("MINEDOCSCAN_DAMAGED_PDF"):
        s.damaged_pdf = env["MINEDOCSCAN_DAMAGED_PDF"]
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, Path(v) if k in ("archive_root", "work_root", "site", "reviews") else v)
    if s.damaged_pdf not in DAMAGED_PDF:
        raise ConfigError(f"[pipeline] damaged_pdf (또는 MINEDOCSCAN_DAMAGED_PDF) 는 {' | '.join(DAMAGED_PDF)}: "
                          f"{s.damaged_pdf!r}")
    return s


def _table(raw: dict, name: str, path: Path) -> dict:
    v = raw.get(name, {})
    if not isinstance(v, dict):
        raise ConfigError(f"[{name}] 는 표여야 합니다 ({path})")
    return v


def _number(table: dict, key: str, default, cast, label: str, lo: float | None = None, hi: float | None = None):
    v = table.get(key, default)
    if isinstance(v, bool):
        raise ConfigError(f"{label} 는 숫자여야 합니다: {v!r}")
    try:
        x = cast(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{label} 는 숫자여야 합니다: {v!r}") from None
    if cast is int and isinstance(v, float) and v != x:
        raise ConfigError(f"{label} 는 정수여야 합니다: {v!r}")
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        rng = f"{lo if lo is not None else '…'} 이상" + (f" {hi} 이하" if hi is not None else "")
        raise ConfigError(f"{label} 는 {rng}: {v!r}")
    return x


def _flag(table: dict, key: str, default: bool, label: str) -> bool:
    v = table.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(f"{label} 는 true | false: {v!r}")
    return v


def _p(v) -> Path | None:
    return Path(v).expanduser() if v else None
