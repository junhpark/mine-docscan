"""실행 설정. 경로는 코드에 적지 않고 설정 파일과 환경변수로만 받는다.

우선순위: 환경변수 > 설정 파일(TOML) > 기본값.

  MINEDOCSCAN_CONFIG        설정 파일 경로 (기본: ./minedocscan.toml)
  MINEDOCSCAN_ARCHIVE_ROOT  스캔 원본 보관 폴더 (읽기 전용으로 취급 — 접수(watch·serve)가 그 아래 intake/ 에만 쓴다, tasks/0007 4.7)
  MINEDOCSCAN_INBOX         접수 폴더 (스캐너 프로그램의 저장 폴더). 다 쓰인 파일을 보관 폴더의 intake/ 로 옮겨 처리한다
  MINEDOCSCAN_WORK_ROOT     작업 폴더 — 정합 이미지, DB, 리포트 (로컬 디스크 권장)
  MINEDOCSCAN_SITE          사이트 팩 폴더 (템플릿·마스터·라벨)
  MINEDOCSCAN_DB_URL        DB 주소 (기본: sqlite:///<work_root>/minedocscan.db)
  MINEDOCSCAN_REVIEWS       검수 기록 파일 (기본: <site>/reviews/reviews.jsonl — 사이트 팩 안, 추가 전용)
  MINEDOCSCAN_DAMAGED_PDF   손상 PDF(라이브러리가 복구해서 연 파일)의 처리: fail(기본) | warn
  MINEDOCSCAN_EXCEL_DIR     엑셀 폴더 (watch·serve 가 바퀴 끝에 쓴다 — tasks/0008 4.7)
  MINEDOCSCAN_PUBLISH_URL   통합 DB(PostgreSQL) — postgresql://사용자:비밀번호@호스트/DB. **환경변수로만** 받는다 (설정 파일에 적지
                            않는다 — 적으면 ConfigError). 어디에도 찍지 않는다 (호스트·DB 이름만 — publish/core.describe_url)
  MINEDOCSCAN_PUBLISH_SCHEMA 통합 DB 의 스키마 (기본 minedocscan)

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
    # 빈 쪽 (tasks/0007 4.5): 양식을 못 찾은 쪽 중 어두운 화소(binarize → 2×2 열기)가 이 비율 미만이면 blank. 0.02 의 근거:
    # 실제 쪽 83장의 최소가 0.046, 합성 흰 종이·티 0.0003 이하, 가장자리 그림자 0.011 이하, 옅게 비친 뒷면(15 %) 0.001 이하.
    # 진하게 비친 뒷면(30 % ≤ 0.028, 45 % ≤ 0.053)은 겹친다 — unknown_form 으로 남아 사람이 본다. 실제 빈 쪽 표본은 아직 없다
    blank_max_ink: float = 0.02
    # 다시 스캔한 쪽 (tasks/0007 4.6): 같은 날·같은 계열의 앞 순서 적재된 쪽과 서명(imaging/signature.py)의 코사인이 이 이상이면 붙잡는다.
    # 실제 3일치, 지금의 서명(표 밖 필드도 지운다 — tasks/0008 1절 라): 같은 날 다른 종이 최대 0.593(286쌍), 다른 날은 0.853 까지.
    # 같은 종이를 흔들어 다시 정합하면 중앙 0.99 지만 **2–3 % 가 0.80 아래**다 (순하게 흔들어 최소 0.700, 거칠게 최소 0.284 — 243쪽씩).
    # 0.70 이면 실데이터에서는 갈리지만 합성(--meta-fields --mix-pages)은 같은 날 다른 종이가 0.771 까지 올라가 기본값은 0.80 그대로.
    # 현장의 값은 실제 다시 스캔을 본 뒤 정한다 (tasks/0007 8절 4)
    dup_min_sim: float = 0.80
    # 접수 폴더 (tasks/0007 4.7): [paths] inbox, [intake] settle_seconds·give_up_seconds·poll_seconds
    inbox: Path | None = None
    settle_seconds: float = 5.0          # 수정 시각이 이만큼 앞이고 열리는 파일만 가져온다 (스캐너가 다 쓰기를 기다린다)
    give_up_seconds: float = 120.0       # 이만큼 지나도 열리지 않으면 손상 방침대로 등록한다 (읽을 수조차 없으면 _failed 로)
    poll_seconds: float = 3.0            # watch·serve 가 접수 폴더를 훑는 간격
    # 엑셀 내보내기 (tasks/0008 4.7): [export] excel_dir (또는 MINEDOCSCAN_EXCEL_DIR) — 없으면 자동 내보내기는 꺼져 있다
    excel_dir: Path | None = None
    export_sweep_minutes: float = 30.0   # 전체 훑기의 간격 (분). 0 이면 시작할 때만 — 다른 프로세스가 쓴 검수와 놓친 것을 잡는다
    machine_values: bool = False         # 업무 시트·긴 표에 "기계 값(확정 아님)" 열을 따로 둔다 (기본은 싣지 않는다 — ADR 0008)
    # 통합 DB 로 싣기 (tasks/0008 4.8): URL 은 환경변수로만 (repr 에도 나오지 않게). enabled 가 None 이면 URL 이 있을 때 켜진다
    publish_url: str | None = field(default=None, repr=False)
    publish_schema: str = "minedocscan"
    publish_enabled: bool | None = None
    publish_sweep_minutes: float = 30.0  # 전체 훑기의 간격 (분). 0 이면 시작할 때만
    publish_connect_timeout_s: float = 5.0
    publish_retry_seconds: float = 60.0  # 연결에 실패한 뒤 이만큼은 다시 연결하지 않는다 (꺼진 서버에 바퀴마다 매달리지 않게)
    extra: dict = field(default_factory=dict)

    @property
    def publish_on(self) -> bool:
        """싣기가 켜져 있나: URL 이 있고 [publish] enabled 가 false 가 아니다."""
        return bool(self.publish_url) and self.publish_enabled is not False

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

    def decisions_path(self, site_root: Path | None = None) -> Path:
        """문서·쪽의 결정 기록 (tasks/0007 4.3): 검수 파일과 같은 폴더의 decisions.jsonl — 사이트 팩 안, 추가 전용."""
        return self.reviews_path(site_root).with_name("decisions.jsonl")


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
    intake = _table(raw, "intake", path)
    export = _table(raw, "export", path)
    publish = _table(raw, "publish", path)
    if any(k in publish for k in ("url", "dsn", "password")):
        raise ConfigError("[publish] 에 URL·비밀번호를 적지 않습니다 — 환경변수 MINEDOCSCAN_PUBLISH_URL 로만 받습니다")
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
        blank_max_ink=_number(pipe, "blank_max_ink", 0.02, float, "[pipeline] blank_max_ink", lo=0.0, hi=1.0),
        dup_min_sim=_number(pipe, "dup_min_sim", 0.80, float, "[pipeline] dup_min_sim", lo=0.0, hi=1.0),
        inbox=_p(paths.get("inbox")),
        settle_seconds=_number(intake, "settle_seconds", 5.0, float, "[intake] settle_seconds", lo=0.0),
        give_up_seconds=_number(intake, "give_up_seconds", 120.0, float, "[intake] give_up_seconds", lo=0.0),
        poll_seconds=_number(intake, "poll_seconds", 3.0, float, "[intake] poll_seconds", lo=0.1),
        excel_dir=_p(export.get("excel_dir")),
        export_sweep_minutes=_number(export, "sweep_minutes", 30.0, float, "[export] sweep_minutes", lo=0.0),
        machine_values=_flag(export, "machine_values", False, "[export] machine_values"),
        publish_schema=str(publish.get("schema", "minedocscan")),
        publish_enabled=(None if "enabled" not in publish else _flag(publish, "enabled", True, "[publish] enabled")),
        publish_sweep_minutes=_number(publish, "sweep_minutes", 30.0, float, "[publish] sweep_minutes", lo=0.0),
        publish_connect_timeout_s=_number(publish, "connect_timeout_s", 5.0, float, "[publish] connect_timeout_s", lo=1.0,
                                          hi=600.0),
        publish_retry_seconds=_number(publish, "retry_seconds", 60.0, float, "[publish] retry_seconds", lo=0.0),
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
    if env.get("MINEDOCSCAN_INBOX"):
        s.inbox = Path(env["MINEDOCSCAN_INBOX"])
    if env.get("MINEDOCSCAN_EXCEL_DIR"):
        s.excel_dir = Path(env["MINEDOCSCAN_EXCEL_DIR"])
    if env.get("MINEDOCSCAN_PUBLISH_URL"):
        s.publish_url = env["MINEDOCSCAN_PUBLISH_URL"]
    if env.get("MINEDOCSCAN_PUBLISH_SCHEMA"):
        s.publish_schema = env["MINEDOCSCAN_PUBLISH_SCHEMA"]
    if env.get("MINEDOCSCAN_DAMAGED_PDF"):
        s.damaged_pdf = env["MINEDOCSCAN_DAMAGED_PDF"]
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, Path(v) if k in ("archive_root", "work_root", "site", "reviews", "inbox", "excel_dir") else v)
    import re

    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", s.publish_schema):
        raise ConfigError("[publish] schema (또는 MINEDOCSCAN_PUBLISH_SCHEMA) 는 영문 소문자·숫자·밑줄 "
                          f"(영문 소문자나 밑줄로 시작, 63자 안): {s.publish_schema!r}")
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
