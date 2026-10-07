"""엑셀 폴더에 쓰기 (tasks/0008 4.6): 내용의 해시, 기록 파일, 원자적으로.

- **바뀌었나**는 모델의 해시로 본다 (파일의 바이트가 아니다). 기록 파일 OUT/.minedocscan-export.json 에 파일마다 해시·판·쓴 시각.
  해시와 판이 같으면 그 파일을 건드리지 않는다 (수정 시각이 그대로다). 기록 파일이 없거나 깨졌으면 전부 다시 쓴다.
- **쓰는 규칙**: 이름 규칙(daily/<YYYY-MM>/<날짜>.xlsx, monthly/<달>.xlsx)에 맞는 경로는 이 프로그램의 것이다.
- **지우는 규칙**: 쪽이 하나도 남지 않은 날짜(달)의 파일은 지운다 — 기록 파일에 있는 것만 (기록을 잃었으면 지우지 않고 수로 알린다).
  사용자가 그 폴더에 둔 다른 파일은 건드리지 않는다.
- **원자적으로**: 같은 폴더의 임시 이름(.xlsx 가 아니다)에 쓰고 os.replace. 바꾸지 못하면(윈도우: 엑셀이 그 파일을 열고 있다)
  임시 파일을 치우고 "쓰지 못함"으로 센다 — 다음에 다시 한다. 예외로 죽지 않는다.
- OUT 은 **있어야 한다** — 없으면 만들지 않는다 (끊긴 네트워크 폴더의 자리에 로컬 폴더를 만들지 않게). 그 아래는 만든다.
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

    def as_dict(self) -> dict:
        return {"written": len(self.written), "unchanged": self.unchanged, "deleted": len(self.deleted),
                "failed": len(self.failed), "kept": self.kept, "skipped_dates": self.skipped_dates,
                "missing_dir": self.missing_dir, "record_lost": self.record_lost}


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
    from ..intake.inbox import _inside, _norm

    for key, why in (("inbox", "접수 폴더 안입니다 — 엑셀을 스캔으로 접수하게 됩니다"),
                     ("archive_root", "보관 폴더(스캔 원본) 안입니다 — 보관 폴더에는 intake/ 아래에만 씁니다")):
        other = getattr(settings, key, None) if settings is not None else None
        if other is not None and (_norm(out) == _norm(other) or _inside(_norm(out), _norm(other))):
            raise ExportError(f"{out} 은 {why}")
    return out


# ── 기록 파일 ───────────────────────────────────────────────────────────────
class RecordUnreadable(OSError):
    """기록 파일이 있는데 읽지 못했다 (잠겨 있다 …) — 이번 내보내기를 하지 않는다 (기록을 덮어쓰면 다른 파일의 기록을 잃는다)."""


def load_record(out: Path) -> tuple[dict, bool]:
    """(파일 → {sha, model, written_at}, 잃었나). 없거나 깨졌으면(JSON 이 아니다) ({}, True) — 전부 다시 쓴다 (4.6).
    있는데 읽지 못하면(잠겨 있다) RecordUnreadable."""
    p = out / RECORD_NAME
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, True
    except OSError as e:
        raise RecordUnreadable(str(e)) from e
    try:
        data = json.loads(raw)
        files = data["files"]
        if not isinstance(files, dict):
            raise ValueError
        return {k: v for k, v in files.items() if isinstance(v, dict)}, False
    except (ValueError, KeyError, TypeError):
        return {}, True


def save_record(out: Path, files: dict) -> None:
    p = out / RECORD_NAME
    tmp = out / f"{RECORD_NAME}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps({"model": MODEL_VERSION, "files": dict(sorted(files.items()))}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
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


def existing_files(out: Path) -> set[str]:
    """OUT 아래에서 이름 규칙에 맞는 파일 (기록을 잃었을 때 지우지 않고 셀 것 — 4.6)."""
    found = set()
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
                 machine_values: bool = False, made_at: str | None = None) -> Result:
    """days·months: 다시 볼 날짜·달 (쪽이 없으면 그 파일을 지운다). full: 전부 훑기 (DB 의 날짜·달 전부 + 기록 파일의 것 + 폴더에
    있는 이름 규칙의 파일). 기록 파일이 없거나 깨졌으면 전부 훑는다 (전부 다시 쓴다).
    파일마다 모델을 만들고 → 해시가 다르면 쓰고 → 버린다 (모델을 다 들고 있지 않게) — 전부 한 읽기 트랜잭션 안에서 (한 시점).
    돌려주는 값: Result (값·이름 없이 수와 경로)."""
    out = Path(out)
    res = Result()
    if not out.is_dir():
        res.missing_dir = True
        return res
    try:
        record, res.record_lost = load_record(out)
    except RecordUnreadable:
        res.failed.append(RECORD_NAME)                       # 다음에 다시 — 기록을 덮어쓰지 않는다
        return res
    full = full or res.record_lost
    made_at = made_at or now_iso()
    with read_txn(con):
        all_days = page_days(con)
        iso = {d for d in all_days if is_iso_day(d)}
        res.skipped_dates = len(set(all_days) - iso)
        for rel, build in _targets(con, site, out, iso, record, days, months, full, machine_values):
            _apply(out, rel, build, record, res, made_at)
            if not out.is_dir():                              # 쓰는 도중에 폴더가 사라졌다 — 그 자리에 만들지 않는다
                res.missing_dir = True
                return res
    if res.written or res.deleted or res.record_lost:
        try:
            save_record(out, record)
        except OSError:
            res.failed.append(RECORD_NAME)
    return res


def _targets(con, site, out: Path, iso: set[str], record: dict, days, months, full: bool, machine_values: bool):
    """(경로, 모델을 만드는 함수 | None) — None 이면 그 날짜(달)에 쪽이 없다 (그 파일을 지운다). 날짜 순, 일별 다음 월별."""
    want = set(iso) if full else {d for d in (days or ()) if is_iso_day(d)}
    if full:
        known = set(record) | existing_files(out)
        want |= {m.group(2) for k in known if (m := DAILY_RE.match(k))}
    for day in sorted(want):
        yield daily_path(day), ((lambda day=day: daily_book(con, site, day, machine_values)) if day in iso else None)
    yield from _monthly_targets(con, site, out, iso, record, months, full, machine_values)


def _monthly_targets(con, site, out: Path, iso: set[str], record: dict, months, full: bool, machine_values: bool):
    """월별 파일: 그 달에 쪽이 있는 날짜를 모은다. full 이면 DB 의 달 전부와 기록·폴더의 달, 아니면 준 달."""
    from .monthly import monthly_book

    by_month: dict[str, list[str]] = {}
    for d in sorted(iso):
        by_month.setdefault(d[:7], []).append(d)
    want = set(by_month) if full else {m for m in (months or ()) if MONTH_RE.match(m)}
    if full:
        want |= {m.group(1) for k in set(record) | existing_files(out) if (m := MONTHLY_RE.match(k))}
    for m in sorted(want):
        yield monthly_path(m), ((lambda m=m: monthly_book(con, site, m, by_month[m], machine_values)) if m in by_month else None)


def _apply(out: Path, rel: str, build, record: dict, res: Result, made_at: str) -> None:
    """파일 하나: 쪽이 없으면 지우고(기록에 있는 것만), 있으면 모델의 해시가 다를 때만 쓴다."""
    path = out / rel
    if build is None:
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
    parts = [f"쓴 파일 {len(r.written)}", f"그대로 {r.unchanged}"]
    if r.deleted:
        parts.append(f"지운 파일 {len(r.deleted)}")
    if r.failed:
        parts.append(f"쓰지 못함 {len(r.failed)} (열려 있는 파일 — 다음에 다시 합니다)")
    if r.kept:
        parts.append(f"기록에 없어 지우지 않은 파일 {r.kept}")
    if r.skipped_dates:
        parts.append(f"ISO 가 아닌 날짜 {r.skipped_dates} (파일로 만들지 않음)")
    if r.record_lost:
        parts.append("기록 파일이 없거나 깨져 전부 다시 씀")
    lines = ["엑셀: " + ", ".join(parts)]
    lines += [f"  {p}" for p in r.written]
    lines += [f"  지움 {p}" for p in r.deleted]
    lines += [f"  쓰지 못함 {p}" for p in r.failed]
    return "\n".join(lines)
