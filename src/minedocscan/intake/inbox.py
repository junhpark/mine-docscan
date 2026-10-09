"""접수 폴더 (tasks/0007 4.7): 스캐너 프로그램의 저장 폴더에서 다 쓰인 파일만 가져와 보관 폴더로 옮기고 문서를 등록한다.

  보는 것     접수 폴더와 하위 폴더의 SUPPORTED_EXT 파일. "_" 로 시작하는 폴더, "." · "~" 로 시작하는 파일은 보지 않는다
  다 쓰였나   수정 시각이 지금보다 settle_seconds 이상 앞이고, 열린다 (PDF 는 마지막 1 KB 에 %%EOF 까지, 이미지는 디코딩 — JPEG 는 끝 표시까지).
              계속 도는 감시(watch·serve)는 지난 바퀴에 잰 크기와 같아야 한다 (수정 시각을 옛것 그대로 두고 복사하는 프로그램이 있다).
              다 쓰이지 않았어도(열리지 않는다, PDF 의 끝 표시가 없다) 수정 시각이 give_up_seconds 이상 앞이면 손상 방침(damaged_pdf)대로
              등록한다 (fail 이면 그 문서는 failed)
  접수        해시 → 이미 있는 문서(같은 바이트)면 <inbox>/_already/ 로 옮기고 DB 는 건드리지 않는다. 새 문서면
              archive_root/intake/<받은 해-달>/<받은 시각>-<document_id>/<원래 파일명> 으로 복사하고(임시 이름 → 이름 바꾸기) 해시를
              다시 확인한 뒤 등록·커밋하고, 그다음에 접수 폴더의 것을 치운다 (끊겨도 바이트가 한 곳 이상에 있다)
  읽을 수 없다 (잠겨 있다) 그대로 두고 다시 본다. give_up_seconds 뒤에도 그러면 <inbox>/_failed/ 로 옮긴다

**지우지 않는다**: 접수 폴더에서 사라진 파일은 보관 폴더, _already, _failed 중 한 곳에 바이트 그대로 있다. 옮기거나 지우다 실패하면
(윈도우: 열려 있는 파일) 다음 바퀴에 다시 한다. 받은 시각은 UTC, 폭이 고정된 YYYYMMDDThhmmssfffZ — 한 바퀴 안에서는 (수정 시각, 이름)
순서로 접수하고, 지금까지 접수한 것 중 가장 늦은 시각(DB) 이후로 민다 (문서의 순서 = 폴더 이름의 순서 — store/order.py).
시계는 주입한다 (now: 수정 시각과 견주는 지금, wall: 받은 시각) — 시험은 잠들지 않는다. 로그·요약에는 수와 문서 ID 만.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..imaging.io import IMAGE_EXT, SUPPORTED_EXT, count_pages, imread_gray
from ..store.order import INTAKE_DIR, document_id

TEMP_SUFFIX = ".part"                    # 복사 중인 임시 파일 — SUPPORTED_EXT 가 아니라 run 이 줍지 않는다
ALREADY, FAILED = "_already", "_failed"
_TS = re.compile(r"^(?P<ts>\d{8}T\d{9}Z)-(?P<doc>[0-9a-f]{16})$")


class InboxError(ValueError):
    """접수 폴더를 쓸 수 없다 (한 줄 — 시작하지 않는다)."""


def check_paths(inbox: Path, archive_root: Path | None, others: dict[str, Path | None]) -> None:
    """접수 폴더가 쓸 만한가: archive_root 가 있어야 하고, 접수 폴더가 archive_root·work_root·사이트 팩의 안이거나 그것들을 품으면
    안 된다 (보관 폴더의 파일을 다시 접수하거나, 접수가 작업 폴더를 옮기게 된다). 윈도우에서는 대소문자를 가리지 않고 견준다."""
    if archive_root is None:
        raise InboxError("접수 폴더를 쓰려면 archive_root 가 있어야 합니다 (보관 폴더 — [paths] archive_root)")
    me = _norm(inbox)
    for name, other in {"archive_root": archive_root, **others}.items():
        if other is None:
            continue
        o = _norm(other)
        if me == o or _inside(me, o) or _inside(o, me):
            raise InboxError(f"접수 폴더가 {name} 와 겹칩니다 — 안이거나 그것을 품으면 안 됩니다: {inbox}")


def _norm(p: Path) -> str:
    return os.path.normcase(str(Path(p).resolve()))


def _inside(a: str, b: str) -> bool:
    return a.startswith(b.rstrip(os.sep) + os.sep)


# ── 받은 시각 ──────────────────────────────────────────────────────────────
def ts_of(ms: int) -> str:
    """에포크 밀리초 → 폴더 이름의 받은 시각 YYYYMMDDThhmmssfffZ (UTC)."""
    d = datetime.fromtimestamp(ms // 1000, UTC)
    return f"{d:%Y%m%dT%H%M%S}{ms % 1000:03d}Z"


def ms_of_ts(ts: str) -> int:
    d = datetime.strptime(ts[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    return int(d.timestamp()) * 1000 + int(ts[15:18])


def received_at_of(ms: int) -> str:
    """에포크 밀리초 → DB 의 received_at 표기 (YYYY-MM-DDTHH:MM:SS.fffZ — Pipeline.register 와 같다)."""
    d = datetime.fromtimestamp(ms // 1000, UTC)
    return f"{d:%Y-%m-%dT%H:%M:%S}.{ms % 1000:03d}Z"


def ms_of_received(v: str | None) -> int | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(round(d.timestamp() * 1000))


def received_from_rel(rel: str | None) -> str | None:
    """보관 경로 intake/<해-달>/<받은 시각>-<문서 ID>/<이름> → received_at (DB 를 지우고 보관 폴더를 다시 돌릴 때). 아니면 None."""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p]
    if len(parts) < 4 or parts[0] != INTAKE_DIR:
        return None
    m = _TS.match(parts[2])
    return received_at_of(ms_of_ts(m["ts"])) if m else None


# ── 접수 ───────────────────────────────────────────────────────────────────
class Inbox:
    """접수 폴더 하나. continuous=True(watch·serve)면 바퀴 사이에 잰 크기를 기억한다 — 크기가 그대로여야 다 쓰인 것으로 본다.
    now: 수정 시각과 견주는 지금(에포크 초), wall: 받은 시각(에포크 초) — 기본은 time.time. 시험이 주입한다."""

    def __init__(self, settings, con: sqlite3.Connection, continuous: bool = False, now: Callable[[], float] | None = None,
                 wall: Callable[[], float] | None = None, settle_seconds: float | None = None,
                 give_up_seconds: float | None = None):
        if settings.inbox is None:
            raise InboxError("접수 폴더가 없습니다 ([paths] inbox 또는 MINEDOCSCAN_INBOX)")
        check_paths(settings.inbox, settings.archive_root, {"work_root": settings.work_root, "site": settings.site})
        self.settings, self.con = settings, con
        self.root = Path(settings.inbox)
        self.archive = Path(settings.archive_root)
        self.settle = settings.settle_seconds if settle_seconds is None else settle_seconds
        self.give_up = settings.give_up_seconds if give_up_seconds is None else give_up_seconds
        self.now = now or time.time
        self.wall = wall or time.time
        self.sizes: dict[str, int] | None = {} if continuous else None
        self.first_seen: dict[str, float] = {}
        self._last_ms: int | None = None

    def files(self) -> list[Path]:
        """보는 파일 — (수정 시각, 이름) 순서."""
        out = []
        if not self.root.is_dir():
            return out
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("_"))
            for f in filenames:
                p = Path(dirpath) / f
                if f.startswith((".", "~")) or p.suffix.lower() not in SUPPORTED_EXT:
                    continue
                try:
                    out.append((p.stat().st_mtime, p.name, str(p), p))
                except OSError:
                    out.append((float("inf"), p.name, str(p), p))      # 잠겨 있다 — 뒤로, 그래도 본다
        return [p for *_k, p in sorted(out)]

    def round(self, pipe) -> dict:
        """한 바퀴: 다 쓰인 파일을 접수하고 등록한다. 돌려주는 값 — 수와 문서 ID 만:
        {"received": [문서 ID], "already": n, "failed": [문서 ID] (등록에서 failed), "moved_failed": n (_failed 로),
         "waiting": n (아직 다 쓰이지 않았다), "retry": n (옮기다 실패 — 다음 바퀴에)}."""
        out = {"received": [], "already": 0, "failed": [], "moved_failed": 0, "waiting": 0, "retry": 0}
        seen = set()
        for path in self.files():
            key = str(path)
            seen.add(key)
            try:
                verdict = self._ready(path)
                if verdict == "wait":
                    out["waiting"] += 1
                    continue
                if verdict == "unreadable":
                    self._move(path, FAILED)
                    out["moved_failed"] += 1
                    continue
                kind, doc = self._ingest(path, pipe)
            except OSError:
                out["retry"] += 1                                       # 윈도우: 열려 있는 파일 — 다음 바퀴에 다시
                continue
            if kind == "already":
                out["already"] += 1
            elif kind == "failed":
                out["failed"].append(doc)
            else:
                out["received"].append(doc)
        if self.sizes is not None:                                     # 사라진 파일의 기억은 버린다
            self.sizes = {k: v for k, v in self.sizes.items() if k in seen}
            self.first_seen = {k: v for k, v in self.first_seen.items() if k in seen}
        return out

    def _age(self, path: Path, mtime: float) -> float:
        now = self.now()
        key = str(path)
        self.first_seen.setdefault(key, now)
        if mtime > now:                                                # 수정 시각이 앞날이다 (공유 폴더의 시계가 틀렸다)
            return now - self.first_seen[key] if self.sizes is not None else self.settle
        return now - mtime

    def _ready(self, path: Path) -> str:
        """"ok"(접수) · "wait"(아직) · "unreadable"(읽을 수 없는 채로 give_up 이 지났다 — _failed 로). 열리지 않아도 give_up 이 지났으면
        "ok" — 등록이 손상 방침대로 정한다."""
        try:
            st = path.stat()
        except OSError:
            return "wait"
        age = self._age(path, st.st_mtime)
        if age < self.settle:
            return "wait"
        if self.sizes is not None:                                     # 계속 도는 감시: 지난 바퀴의 크기와 같아야 한다
            prev, self.sizes[str(path)] = self.sizes.get(str(path)), st.st_size
            if prev != st.st_size:
                return "wait"
        try:
            with open(path, "rb"):
                pass
        except OSError:
            return "unreadable" if age >= self.give_up else "wait"
        if complete(path):
            return "ok"
        return "ok" if age >= self.give_up else "wait"

    def _ingest(self, path: Path, pipe) -> tuple[str, str]:
        data = path.read_bytes()
        doc = document_id(data)
        if self.con.execute("SELECT 1 FROM doc_document WHERE document_id = ?", (doc,)).fetchone() is not None:
            self._move(path, ALREADY, doc)                             # 같은 바이트 — DB 는 그대로
            return "already", doc
        dest = self._archived(doc, path.name) or self._copy(path, data, doc)
        rel = dest.relative_to(self.archive).as_posix()
        out = pipe.register(dest, document_id=doc, source_name=Path(path.name).stem, received_at=received_from_rel(rel),
                            source_rel=rel)
        if self.con.in_transaction:
            self.con.commit()
        path.unlink()                                                  # 보관 폴더의 사본을 해시로 확인했고 등록했다 — 그다음에 치운다
        return ("failed" if out["status"] == "failed" else "received"), doc

    def _archived(self, doc: str, name: str) -> Path | None:
        """끊긴 접수가 남긴 사본 (intake/*/*-<문서 ID>/<이름>, 해시가 맞는 것) — 있으면 그것을 쓴다 (같은 바이트를 두 번 보관하지 않게)."""
        base = self.archive / INTAKE_DIR
        if not base.is_dir():
            return None
        for folder in sorted(base.glob(f"*/*-{doc}")):
            p = folder / name
            if p.is_file() and document_id(p.read_bytes()) == doc:
                return p
        return None

    def _next_ms(self) -> int:
        """받은 시각: 지금, 다만 지금까지 접수한 것 중 가장 늦은 시각(DB) 보다 늦지 않으면 그것의 1 ms 뒤."""
        if self._last_ms is None:
            latest = None
            for (rel, rec) in self.con.execute("SELECT source_rel, received_at FROM doc_document WHERE source_rel LIKE ?",
                                               (f"{INTAKE_DIR}/%",)):
                ms = ms_of_received(received_from_rel(rel) or rec)
                if ms is not None and (latest is None or ms > latest):
                    latest = ms
            self._last_ms = latest
        ms = int(self.wall() * 1000)
        if self._last_ms is not None and ms <= self._last_ms:
            ms = self._last_ms + 1
        self._last_ms = ms
        return ms

    def _copy(self, path: Path, data: bytes, doc: str) -> Path:
        ms = self._next_ms()
        ts = ts_of(ms)
        folder = self.archive / INTAKE_DIR / f"{ts[:4]}-{ts[4:6]}" / f"{ts}-{doc}"
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (path.name + TEMP_SUFFIX)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        dest = folder / path.name
        os.replace(tmp, dest)
        if document_id(dest.read_bytes()) != doc:      # 복사한 것을 다시 확인한다
            raise OSError("보관 폴더의 사본이 원본과 다릅니다")
        return dest

    def _move(self, path: Path, where: str, doc: str | None = None) -> Path:
        """<inbox>/_already·_failed 로 옮긴다 — 이름이 겹치면 문서 ID 나 번호를 붙인다 (덮어쓰지 않는다)."""
        target_dir = self.root / where
        target_dir.mkdir(parents=True, exist_ok=True)
        stem, suf = Path(path.name).stem, Path(path.name).suffix
        cands = [path.name] + ([f"{stem}-{doc}{suf}"] if doc else []) + [f"{stem}-{doc or 'x'}-{i}{suf}" for i in range(2, 1000)]
        for name in cands:
            dest = target_dir / name
            if not dest.exists():
                try:
                    os.rename(path, dest)                              # 같은 볼륨 — 덮어쓰지 않는 이름 바꾸기
                except FileExistsError:
                    continue
                return dest
        raise OSError("옮길 이름이 없습니다")


def complete(path: Path) -> bool:
    """다 쓰인 파일인가: PDF 는 열리고 끝 표시(%%EOF)가 있다 (손상 방침 fail 그대로 — tasks/0009 4.3), 이미지는 디코딩된다
    (JPEG 는 끝 표시 FF D9 까지 — OpenCV 4.9 는 잘린 JPEG 도 디코딩한다). 여러 쪽 TIFF 처럼 등록이 거절할 것은 "다 쓰였다"로 보고
    등록에 맡긴다."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            count_pages(path, "fail")
            return True
        if ext in (".jpg", ".jpeg"):
            data = path.read_bytes().rstrip(b"\x00\r\n ")
            if not data.endswith(b"\xff\xd9"):
                return False
        if ext in IMAGE_EXT:
            imread_gray(path)
            return True
    except Exception:                                                  # noqa: BLE001 — 열리지 않는다 = 아직
        return False
    return False


__all__ = ["ALREADY", "FAILED", "Inbox", "InboxError", "check_paths", "complete", "received_from_rel"]
