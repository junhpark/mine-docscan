"""엑셀 폴더에 쓰기 (tasks/0008 4.6): 내용의 해시, 기록 파일, 원자적으로.

- **바뀌었나**는 모델의 해시로 본다 (파일의 바이트가 아니다). 기록 파일 OUT/.minedocscan-export.json 에 파일마다 해시·판·쓴 시각.
  해시와 판이 같으면 그 파일을 건드리지 않는다 (수정 시각이 그대로다). 기록 파일이 없거나 깨졌으면 전부 다시 쓴다.
- **쓰는 규칙**: 이름 규칙(daily/<YYYY-MM>/<날짜>.xlsx, monthly/<달>.xlsx)에 맞는 경로는 이 프로그램의 것이다.
- **지우는 규칙**: 쪽이 하나도 남지 않은 날짜(달)의 파일은 지운다 — 기록 파일에 있는 것만 (기록을 잃었으면 지우지 않고 수로 알린다).
  사용자가 그 폴더에 둔 다른 파일은 건드리지 않는다.
- **원자적으로**: 같은 폴더의 임시 이름(.xlsx 가 아니다)에 쓰고 os.replace. 바꾸지 못하면(윈도우: 엑셀이 그 파일을 열고 있다)
  임시 파일을 치우고 "쓰지 못함"으로 센다 — 다음에 다시 한다. 예외로 죽지 않는다.
- OUT 은 **있어야 한다** — 없으면 만들지 않는다 (끊긴 네트워크 폴더의 자리에 로컬 폴더를 만들지 않게). 그 아래는 만든다.
- **사본의 주인** (tasks/0009 4.1 다): 기록 파일에 사이트 팩의 [site] name 을 둔다. 다른 사이트의 기록이면 아무것도 쓰거나 지우지 않는다
  (Result.other_site). 기록에 사이트가 없으면(옛 기록) 받아들이고 적는다. [site] name 이 없는 사이트 팩이면 ExportError.
  작업 DB 에 쪽이 하나도 없으면 기록된 파일도 지우지 않는다 (빈 작업 DB 로 훑어 폴더를 비우지 않게 — "남겨 둔 파일"로 센다).
- 작업 DB 에는 아무것도 쓰지 않는다 (읽기 트랜잭션 하나로 모델을 만든다 — store.db.read_txn).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__
from ..store.db import read_txn
from .daily import daily_book
from .model import MODEL_VERSION, book_hash, is_iso_day
from .xlsx import write_book

RECORD_NAME = ".minedocscan-export.json"
_replace = os.replace                      # 엑셀 파일을 바꿔 넣는 것 (시험이 "열려 있는 파일"을 흉내 낼 때 이것만 바꾼다)
DAILY_RE = re.compile(r"^daily/(\d{4}-\d{2})/(\d{4}-\d{2}-\d{2})\.xlsx\Z", re.ASCII)
MONTHLY_RE = re.compile(r"^monthly/(\d{4}-\d{2})\.xlsx\Z", re.ASCII)
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])\Z", re.ASCII)


class ExportError(ValueError):
    """내보낼 곳을 쓸 수 없다 (한 줄)."""


NO_SITE_NAME = ("사이트 팩의 site.toml 에 [site] name 이 없습니다 — 엑셀 폴더·통합 DB 가 어느 사이트의 사본인지 적는 이름입니다 "
                "(폴더 이름으로 대신하지 않습니다). 적기 전에 minedocscan info 가 평가셋의 소금값을 알리면 그것부터 적습니다")


def site_name(site) -> str:
    """사이트 팩의 [site] name (적혀 있는 것만). 없으면 ExportError."""
    name = getattr(site, "declared_name", None)
    if not name:
        raise ExportError(NO_SITE_NAME)
    return name


@dataclass
class Result:
    written: list[str] = field(default_factory=list)          # OUT 기준 경로
    unchanged: int = 0
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)           # 쓰지 못함 (다음에 다시)
    kept: int = 0                                             # 지울 파일인데 기록에 없어 남겼다 (기록을 잃었다)
    skipped_dates: int = 0                                    # ISO 날짜가 아닌 work_date — 파일로 만들지 않는다 (4.3)
    missing_dir: bool = False                                 # OUT 이 없다
    record_lost: bool = False
    other_site: bool = False                                  # 기록 파일이 다른 사이트의 것 — 아무것도 쓰거나 지우지 않았다
    deferred: list[str] = field(default_factory=list)         # 바꾸지 못해 retry_seconds 를 기다리는 파일 (이번에는 건드리지 않았다)
    failed_changed: bool = True                               # 쓰지 못한 파일의 수가 바뀌었다 (바퀴의 요약은 그때만 — 4.2 라)
    empty_db: bool = False                                    # 작업 DB 에 쪽이 없다 — 기록된 파일도 지우지 않았다 (kept 로 센다)
    failing: int | None = None                                # 바퀴 끝의 내보내기: 지금 쓰지 못하고 있는 파일의 수 (기다리는 것 포함)

    def as_dict(self) -> dict:
        return {"written": len(self.written), "unchanged": self.unchanged, "deleted": len(self.deleted),
                "failed": len(self.failed), "kept": self.kept, "skipped_dates": self.skipped_dates,
                "missing_dir": self.missing_dir, "record_lost": self.record_lost, "other_site": self.other_site,
                "empty_db": self.empty_db, "deferred": len(self.deferred), "failed_changed": self.failed_changed,
                **({"failing": self.failing} if self.failing is not None else {})}


def daily_path(day: str) -> str:
    return f"daily/{day[:7]}/{day}.xlsx"


def monthly_path(month: str) -> str:
    return f"monthly/{month}.xlsx"


def inside_git_tree(path: Path) -> bool:
    from ..review.export import inside_git_tree as inside

    return inside(path)


def check_out_dir(out: Path, settings=None, allow_in_repo: bool = False) -> Path:
    """내보낼 곳을 확인한다: 저장소 안·접수 폴더 안은 거절 (현장 데이터다 — 4.1). 없는 폴더는 만들지 않는다 (부른 쪽이 알린다)."""
    out = Path(out)
    if inside_git_tree(out) and not allow_in_repo:
        raise ExportError(f"{out} 은 git 작업 트리 안입니다. 엑셀에는 현장의 값(이름·차량번호)이 들어 있으므로 저장소 밖에 내보내세요 "
                          "(합성 데이터만 --allow-in-repo)")
    from ..review.export import inside_intake_folders

    if why := inside_intake_folders(out, settings, "엑셀"):
        raise ExportError(why)
    return out


# ── 기록 파일 ───────────────────────────────────────────────────────────────
class RecordUnreadable(OSError):
    """기록 파일이 있는데 읽지 못했다 (잠겨 있다 …) — 이번 내보내기를 하지 않는다 (기록을 덮어쓰면 다른 파일의 기록을 잃는다)."""


def load_record(out: Path) -> tuple[dict, bool, str | None]:
    """(파일 → {sha, model, written_at}, 잃었나, 사이트 이름 | None). 없거나 깨졌으면 ({}, True, None) — 전부 다시 쓴다 (0008 4.6).
    깨졌다 = 바이트를 UTF-8 로 디코딩하지 못한다(다른 인코딩으로 다시 저장했다 — tasks/0009 4.1 나)·JSON 이 아니다·꼴이 다르다.
    있는데 읽지 못하면(잠겨 있다) RecordUnreadable."""
    p = out / RECORD_NAME
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return {}, True, None
    except OSError as e:
        raise RecordUnreadable(str(e)) from e
    try:
        data = json.loads(raw.decode("utf-8"))
        files = data["files"]
        if not isinstance(files, dict):
            raise ValueError
        site = data.get("site")
        if site is not None and not (isinstance(site, str) and site.strip()):
            raise ValueError                                  # 사이트 자리가 글자가 아니거나 비었다 — 깨진 기록 (옛 기록은 키가 없다)
        return {k: v for k, v in files.items() if isinstance(v, dict)}, False, site
    except (ValueError, KeyError, TypeError, AttributeError):      # UnicodeDecodeError 는 ValueError
        return {}, True, None


def save_record(out: Path, files: dict, site: str) -> None:
    p = out / RECORD_NAME
    tmp = out / f"{RECORD_NAME}.{os.getpid()}.tmp"
    try:
        tmp.write_bytes(json.dumps({"model": MODEL_VERSION, "site": site, "files": dict(sorted(files.items()))},
                                   ensure_ascii=False, indent=1).encode("utf-8"))
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_dirs(out: Path, rel: str) -> Path:
    """OUT 아래의 폴더만 한 단계씩 만든다 — OUT 자신은 만들지 않는다 (끊긴 네트워크 폴더의 자리에 로컬 폴더를 만들지 않게 — 4.6)."""
    if not out.is_dir():
        raise FileNotFoundError(f"엑셀 폴더가 없습니다: {out}")
    d = out
    for part in Path(rel).parent.parts:
        d = d / part
        if not d.is_dir():
            d.mkdir()
    return out / rel


def write_atomic(book: dict, out: Path, rel: str, made_at: str) -> None:
    """같은 폴더의 임시 이름(.xlsx 가 아니다)에 쓰고 바꾼다. 실패하면 임시 파일을 치우고 OSError 를 올린다."""
    path = make_dirs(out, rel)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write_book(book, tmp, made_at, f"minedocscan {__version__} · 모델 {MODEL_VERSION}")
        _replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ── 내보내기 ───────────────────────────────────────────────────────────────
def page_days(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date IS NOT NULL ORDER BY work_date")]


def existing_files(out: Path, month: str | None = None) -> set[str]:
    """OUT 아래에서 이름 규칙에 맞는 파일 (기록을 잃었을 때 지우지 않고 셀 것 — 4.6). month: 그 달의 일별 파일만 (조각 — 4.2 가)."""
    found = set()
    if month is not None:
        d = out / "daily" / month
        for p in d.glob("*.xlsx") if d.is_dir() else ():
            rel = p.relative_to(out).as_posix()
            if (m := DAILY_RE.match(rel)) and m.group(2)[:7] == m.group(1):
                found.add(rel)
        return found
    for p in (out / "daily").glob("*/*.xlsx") if (out / "daily").is_dir() else ():
        rel = p.relative_to(out).as_posix()
        m = DAILY_RE.match(rel)
        if m and m.group(2)[:7] == m.group(1):
            found.add(rel)
    for p in (out / "monthly").glob("*.xlsx") if (out / "monthly").is_dir() else ():
        rel = p.relative_to(out).as_posix()
        if MONTHLY_RE.match(rel):
            found.add(rel)
    return found


def export_excel(con: sqlite3.Connection, site, out: str | Path, days=None, months=None, full: bool = False,
                 machine_values: bool = False, made_at: str | None = None, slices=(), skip=(), full_if_lost: bool = True) -> Result:
    """days·months: 다시 볼 날짜·달 (쪽이 없으면 그 파일을 지운다). full: 전부 훑기 (DB 의 날짜·달 전부 + 기록 파일의 것 + 폴더에
    있는 이름 규칙의 파일). 기록 파일이 없거나 깨졌으면 전부 훑는다 (전부 다시 쓴다) — full_if_lost=False 면 준 범위만 (바퀴 끝의
    내보내기: 기록을 잃으면 새 조각 바퀴로 전부 다시 쓴다 — 처음 쓰는 빈 폴더도 조각으로, tasks/0009 4.2 가).
    slices: 전체 훑기의 조각인 달들 (tasks/0009 4.2 가) — 그 달의 날짜 전부(DB 의 쪽, 기록·폴더의 파일)와 그 달의 월별 파일을 더한다.
    조각을 한 바퀴 다 돌면 full 과 같다. 조각·더러운 범위만 볼 때는 그 날짜·달의 행만 읽는다 (DB 의 날짜 전부를 읽지 않는다).
    skip: 이번에는 건드리지 않을 파일 (바꾸지 못해 retry_seconds 를 기다리는 것 — 4.2 라). 모델도 만들지 않는다 (Result.deferred).
    파일마다 모델을 만들고 → 해시가 다르면 쓰고 → 버린다 (모델을 다 들고 있지 않게) — 전부 한 읽기 트랜잭션 안에서 (한 시점).
    사이트 팩에 [site] name 이 없으면 ExportError, 기록 파일이 다른 사이트의 것이면 아무것도 하지 않는다 (Result.other_site).
    돌려주는 값: Result (값·이름 없이 수와 경로)."""
    name = site_name(site)
    out = Path(out)
    res = Result()
    if not out.is_dir():
        res.missing_dir = True
        return res
    try:
        record, res.record_lost, owner = load_record(out)
    except RecordUnreadable:
        res.failed.append(RECORD_NAME)                       # 다음에 다시 — 기록을 덮어쓰지 않는다
        return res
    if owner is not None and owner != name:
        res.other_site = True                                 # 다른 사이트의 폴더 — 쓰지도 지우지도 않는다 (이름을 찍지 않는다)
        return res
    full = full or (res.record_lost and full_if_lost)
    made_at = made_at or now_iso()
    skip = set(skip or ())
    with read_txn(con):
        res.empty_db = con.execute("SELECT 1 FROM doc_page LIMIT 1").fetchone() is None
        if full:
            all_days = page_days(con)
            iso = {d for d in all_days if is_iso_day(d)}
            res.skipped_dates = len(set(all_days) - iso)
            want_days = set(iso) | {m.group(2) for k in set(record) | existing_files(out) if (m := DAILY_RE.match(k))}
            by_month: dict[str, list[str]] = {}
            for d in sorted(iso):
                by_month.setdefault(d[:7], []).append(d)
            want_months = set(by_month) | {m.group(1) for k in set(record) | existing_files(out) if (m := MONTHLY_RE.match(k))}
        else:
            want_days = {d for d in (days or ()) if is_iso_day(d)}
            want_months = {m for m in (months or ()) if MONTH_RE.match(m)}
            for m in (x for x in slices or () if MONTH_RE.match(x)):
                want_months.add(m)
                want_days |= month_days(con, m) | {k.group(2) for rel in set(record) | existing_files(out, m)
                                                   if (k := DAILY_RE.match(rel)) and k.group(1) == m}
            by_month = {m: sorted(month_days(con, m)) for m in want_months}
            iso = days_with_pages(con, want_days)
            # ISO 가 아닌 쪽 날짜는 파일로 만들지 않는다 — 수만 알린다 (전부 훑기와 같이): 준 날짜 가운데와 조각의 달에 든 것
            odd = {d for d in (days or ()) if d and not is_iso_day(d)}
            for m in (x for x in slices or () if MONTH_RE.match(x)):
                odd |= {r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date >= ? AND work_date < ?",
                                                  (f"{m}-", f"{m}.")) if not is_iso_day(r[0])}
            res.skipped_dates = len(days_with_pages(con, odd))
        for rel, build in _targets(con, site, iso, want_days, want_months, by_month, machine_values, skip):
            if rel in skip:
                res.deferred.append(rel)                     # 바꾸지 못해 기다리는 파일 — 이번에는 모델도 만들지 않는다
                continue
            _apply(out, rel, build, record, res, made_at)
            if not out.is_dir():                              # 쓰는 도중에 폴더가 사라졌다 — 그 자리에 만들지 않는다
                res.missing_dir = True
                return res
    if res.written or res.deleted or res.record_lost or owner is None:     # 옛 기록(사이트 없음)에는 사이트를 적는다
        try:
            save_record(out, record, name)
        except OSError:
            res.failed.append(RECORD_NAME)
    return res


def month_days(con: sqlite3.Connection, month: str) -> set[str]:
    """그 달에 쪽이 있는 ISO 날짜 (그 달의 행만 읽는다)."""
    return {r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date >= ? AND work_date < ?",
                                      (f"{month}-", f"{month}-~")) if is_iso_day(r[0])}


def days_with_pages(con: sqlite3.Connection, days) -> set[str]:
    """그 날짜들 가운데 쪽이 있는 것."""
    days = sorted(set(days))
    out: set[str] = set()
    for i in range(0, len(days), 500):
        chunk = days[i:i + 500]
        out |= {r[0] for r in con.execute(f"SELECT DISTINCT work_date FROM doc_page WHERE work_date IN ({','.join('?' * len(chunk))})",
                                          chunk)}
    return out


def odd_page_days(con: sqlite3.Connection) -> int:
    """ISO 날짜가 아닌 쪽 날짜의 수 — 파일로 만들지 않는 것 (4.3). 바퀴 끝의 내보내기가 바퀴를 시작할 때 한 번 센다 (달의 꼴도 아닌
    날짜는 어느 조각에도 들지 않는다)."""
    return sum(1 for d in page_days(con) if not is_iso_day(d))


def months_of(con: sqlite3.Connection, out: Path, record: dict | None = None) -> list[str]:
    """전체 훑기의 조각(달)의 목록: 작업 DB 의 달과 기록 파일·폴더에만 있는 달 (tasks/0009 4.2 가)."""
    found = {r[0] for r in con.execute("SELECT DISTINCT substr(work_date, 1, 7) FROM doc_page WHERE work_date IS NOT NULL")}
    rels = set(record or ()) | existing_files(out)
    found |= {m.group(1) for k in rels if (m := DAILY_RE.match(k) or MONTHLY_RE.match(k))}
    return sorted(m for m in found if m and MONTH_RE.match(m))


def _targets(con, site, iso: set[str], want_days: set[str], want_months: set[str], by_month: dict[str, list[str]],
             machine_values: bool, skip: set[str] = frozenset()):
    """(경로, 모델을 만드는 함수 | None) — None 이면 그 날짜(달)에 쪽이 없다 (그 파일을 지운다). 달마다 그 달의 일별(날짜 순) 다음 월별.
    월별 파일을 만드는 달은 그 달의 쪽을 한 번 읽어 일별 파일과 같이 쓴다 (tasks/0009 4.2 마 — 일별과 월별이 같은 행을 두 번 읽지 않게.
    한 번에 들고 있는 것은 달 하나 — 월별 파일을 만드는 동안 들고 있던 것과 같다)."""
    from .model import load_pages
    from .monthly import monthly_book

    for m in sorted({d[:7] for d in want_days} | set(want_months)):
        held: dict = {}

        def month_pages(m=m, held=held):
            if "pages" not in held:
                held["pages"] = load_pages(con, by_month[m])
            return held["pages"]

        share = m in want_months and bool(by_month.get(m)) and monthly_path(m) not in skip
        in_month = set(by_month.get(m) or ())
        for day in sorted(d for d in want_days if d[:7] == m):
            if day not in iso:
                yield daily_path(day), None
            elif share and day in in_month:
                yield daily_path(day), (lambda day=day, mp=month_pages: daily_book(con, site, day, machine_values, pages=mp().day(day)))
            else:
                yield daily_path(day), (lambda day=day: daily_book(con, site, day, machine_values))
        if m in want_months:
            yield monthly_path(m), ((lambda m=m, mp=month_pages: monthly_book(con, site, m, by_month[m], machine_values, pages=mp()))
                                    if by_month.get(m) else None)


def _apply(out: Path, rel: str, build, record: dict, res: Result, made_at: str) -> None:
    """파일 하나: 쪽이 없으면 지우고(기록에 있는 것만), 있으면 모델의 해시가 다를 때만 쓴다."""
    path = out / rel
    if build is None:
        if res.empty_db:                                      # 쪽이 하나도 없는 작업 DB — 기록된 파일도 남겨 둔다
            if path.exists():
                res.kept += 1
            return
        if rel in record:
            try:
                existed = path.exists()
                path.unlink(missing_ok=True)
            except OSError:
                res.failed.append(rel)
                return
            del record[rel]
            if existed:
                res.deleted.append(rel)
        elif path.exists():
            res.kept += 1                                     # 기록에 없다 — 이 프로그램이 쓴 것인지 모른다. 지우지 않고 센다
        return
    try:
        book = build()
    except Exception:                                         # noqa: BLE001 — 한 파일의 모델이 실패해도(템플릿이 DB 와 어긋났다 …)
        res.failed.append(rel)                                # 나머지 파일과 기록은 쓴다. "쓰지 못함"으로 세고 다음에 다시
        return
    sha = book_hash(book)
    rec = record.get(rel)
    if rec and rec.get("sha") == sha and rec.get("model") == MODEL_VERSION and path.is_file():
        res.unchanged += 1
        return
    try:
        write_atomic(book, out, rel, made_at)
    except Exception:                                         # noqa: BLE001 — 바꾸지 못했다(엑셀이 열고 있다)·쓰지 못했다 — 다음에 다시
        res.failed.append(rel)
        return
    record[rel] = {"sha": sha, "model": MODEL_VERSION, "written_at": made_at}
    res.written.append(rel)


def format_result(r: Result) -> str:
    """사람이 친 명령의 요약 (날짜를 내도 된다 — 값·이름·파일명은 내지 않는다, 4.1)."""
    if r.missing_dir:
        return "엑셀: 내보낼 폴더가 없습니다 — 만들지 않습니다 (설정의 excel_dir 또는 OUT 을 확인하세요)"
    if r.other_site:
        return (f"엑셀: 이 폴더는 다른 사이트 팩의 사본입니다 — 아무것도 쓰거나 지우지 않았습니다 (맞는 폴더인지 확인하고, 이 사이트로 "
                f"바꾸려면 기록 파일 {RECORD_NAME} 을 지우십시오 — 전부 다시 씁니다)")
    parts = [f"쓴 파일 {len(r.written)}", f"그대로 {r.unchanged}"]
    if r.deleted:
        parts.append(f"지운 파일 {len(r.deleted)}")
    if r.failed:
        parts.append(f"쓰지 못함 {len(r.failed)} (열려 있는 파일 — 다음에 다시 합니다)")
    if r.kept:
        parts.append(f"{'작업 DB 에 쪽이 없어' if r.empty_db else '기록에 없어'} 지우지 않은 파일 {r.kept}")
    if r.skipped_dates:
        parts.append(f"ISO 가 아닌 날짜 {r.skipped_dates} (파일로 만들지 않음)")
    if r.record_lost:
        parts.append("기록 파일이 없거나 깨져 전부 다시 씀")
    lines = ["엑셀: " + ", ".join(parts)]
    lines += [f"  {p}" for p in r.written]
    lines += [f"  지움 {p}" for p in r.deleted]
    lines += [f"  쓰지 못함 {p}" for p in r.failed]
    return "\n".join(lines)
