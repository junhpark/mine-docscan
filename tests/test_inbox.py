"""접수 폴더 (tasks/0007 단계 4, 4.7): 다 쓰인 파일만, 보관 폴더로 옮기고, 지우지 않는다. 시계는 주입한다 — 잠들지 않는다.

합성 데이터만 — conftest.BUNDLES 의 2–3쪽짜리 묶음. 렌더링·분류·정합은 저장해 둔 결과를 쓴다 (conftest.fast_imaging).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import fast_imaging, split_pages
from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.forms.sitepack import SitePack
from minedocscan.intake import inbox as ib
from minedocscan.intake.worker import Worker, format_round
from minedocscan.pipeline import Pipeline
from test_reprocess import BUSINESS, SKIP, assert_same, dump, no_null_dates

WALL = 1_893_456_000.0          # 받은 시각의 시계 (2030-01-01T00:00:00Z) — 시험마다 같은 폴더 이름


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class Clock:
    """now: 수정 시각과 견주는 지금, wall: 받은 시각 (밀리초씩 간다)."""

    def __init__(self):
        self.t = time.time()
        self.w = WALL

    def now(self) -> float:
        return self.t

    def wall(self) -> float:
        self.w += 0.001
        return self.w


@pytest.fixture
def box(bundles, tmp_path, monkeypatch):
    fast_imaging(monkeypatch)
    st = Settings(site=bundles["site"], archive_root=tmp_path / "보관", work_root=tmp_path / "work",
                  reviews=tmp_path / "기록" / "reviews.jsonl", inbox=tmp_path / "스캐너", save_aligned=False)
    st.inbox.mkdir()
    st.archive_root.mkdir()
    clock = Clock()
    pipe = Pipeline(st, site=SitePack(bundles["site"]))
    return {"st": st, "pipe": pipe, "clock": clock, "files": bundles["files"], "root": tmp_path, "site": pipe.site}


def inbox_of(box, continuous=False, **kw) -> ib.Inbox:
    c = box["clock"]
    return ib.Inbox(box["st"], box["pipe"].con, continuous=continuous, now=c.now, wall=c.wall, **kw)


def drop(box, name: str, src: str | Path, age: float = 60.0) -> Path:
    """접수 폴더에 파일을 넣는다 (수정 시각 = 지금 − age)."""
    dest = box["st"].inbox / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(box["files"][src] if isinstance(src, str) and src in box["files"] else src, dest)
    t = box["clock"].t - age
    os.utime(dest, (t, t))
    return dest


def visible(box) -> list[str]:
    """접수 폴더에 남은 파일 (_ 폴더 밖)."""
    root = box["st"].inbox
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
                  and not p.relative_to(root).parts[0].startswith("_"))


def everywhere(box) -> dict[str, list[str]]:
    """보관 폴더(intake/)·_already·_failed 에 있는 파일의 해시 → 경로."""
    out: dict[str, list[str]] = {}
    for base in (box["st"].archive_root / "intake", box["st"].inbox / "_already", box["st"].inbox / "_failed"):
        for p in base.rglob("*") if base.exists() else []:
            if p.is_file():
                out.setdefault(sha(p), []).append(str(p))
    return out


# ── 다 쓰였나 ──────────────────────────────────────────────────────────────
def test_settle_then_ingest_and_once_twice_is_once(box):
    """수정 시각이 settle_seconds 안이면 건드리지 않고, 지나면 접수한다. 원래 파일명이 source_name 에 남고 파일명 규칙이 그것에 걸린다.
    한 바퀴를 두 번 돌려도 한 번 돌린 것과 같다."""
    f = drop(box, "a_2030-01-07.pdf", "a_2030-01-07", age=2)
    digest = sha(f)
    w = Worker(box["pipe"], inbox_of(box))
    r = w.run_once()
    assert r["waiting"] == 1 and r["received"] == [] and f.exists()
    assert box["pipe"].con.execute("SELECT COUNT(*) FROM doc_document").fetchone()[0] == 0
    box["clock"].t += 10
    r = w.run_once()
    assert len(r["received"]) == 1 and r["processed"] == 1 and visible(box) == []
    doc = r["received"][0]
    row = box["pipe"].con.execute("SELECT * FROM doc_document WHERE document_id = ?", (doc,)).fetchone()
    assert (row["source_name"], row["work_date"], row["date_source"], row["status"]) == (
        "a_2030-01-07", "2030-01-07", "filename", "needs_review")
    assert row["source_rel"] == f"intake/2030-01/20300101T000000001Z-{doc}/a_2030-01-07.pdf"
    assert row["received_at"] == "2030-01-01T00:00:00.001Z"
    assert sha(box["st"].archive_root / row["source_rel"]) == digest
    once = dump(box["pipe"].con)
    assert w.run_once() == {"received": [], "already": 0, "moved_failed": 0, "waiting": 0, "retry": 0, "too_long": 0,
                            "too_long_changed": False, "processed": 0}
    assert dump(box["pipe"].con) == once
    assert "a_2030-01-07" not in format_round(r) and doc in format_round(r)      # 요약에 파일명이 없다


def test_continuous_watch_also_waits_for_a_stable_size(box):
    """계속 도는 감시는 지난 바퀴에 잰 크기와 같아야 가져온다 (수정 시각을 옛것 그대로 두고 복사하는 프로그램)."""
    drop(box, "a_2030-01-07.pdf", "a_2030-01-07")
    inbox = inbox_of(box, continuous=True)
    assert inbox.round(box["pipe"])["waiting"] == 1                # 처음 본 크기
    assert len(inbox.round(box["pipe"])["received"]) == 1


def test_truncated_pdf_waits_then_fails_and_the_intact_file_is_a_new_document(box):
    """잘린 PDF 는 give_up_seconds 뒤에 failed 문서가 되고 접수 폴더에 남지 않는다. 뒤에 온전한 파일이 오면 새 문서다."""
    data = box["files"]["a_2030-01-07"].read_bytes()
    cut = box["root"] / "cut.pdf"
    cut.write_bytes(data[: len(data) // 2])
    drop(box, "a_2030-01-07.pdf", cut, age=60)
    w = Worker(box["pipe"], inbox_of(box))
    assert w.run_once()["waiting"] == 1 and visible(box) == ["a_2030-01-07.pdf"]
    box["clock"].t += 120
    r = w.run_once()
    assert len(r["failed"]) == 1 and visible(box) == []
    con = box["pipe"].con
    assert con.execute("SELECT status FROM doc_document").fetchall()[0][0] == "failed"
    assert sha(cut) in everywhere(box)
    drop(box, "a_2030-01-07.pdf", "a_2030-01-07")
    r = w.run_once()
    assert len(r["received"]) == 1 and r["received"][0] not in r.get("failed", [])
    assert sorted(x[0] for x in con.execute("SELECT status FROM doc_document")) == ["failed", "needs_review"]


def test_same_bytes_go_to_already_and_names_are_never_overwritten(box):
    """같은 바이트의 파일은 _already 로 가고 DB 가 변하지 않는다. 이름이 겹쳐도 덮어쓰지 않는다."""
    w = Worker(box["pipe"], inbox_of(box))
    drop(box, "a_2030-01-07.pdf", "a_2030-01-07")
    w.run_once()
    before = dump(box["pipe"].con)
    for name in ("a_2030-01-07.pdf", "copy.pdf", "sub/a_2030-01-07.pdf"):
        drop(box, name, "a_2030-01-07")
    r = w.run_once()
    assert r["already"] == 3 and r["received"] == [] and visible(box) == []
    assert dump(box["pipe"].con) == before
    names = sorted(p.name for p in (box["st"].inbox / "_already").iterdir())
    assert len(names) == 3 and "a_2030-01-07.pdf" in names and "copy.pdf" in names


def test_subfolders_korean_names_and_two_files_with_one_name(box):
    """하위 폴더의 파일, 한글 이름의 파일도 접수된다. 같은 이름의 다른 파일 둘은 두 문서다. _ 폴더·./~ 파일은 보지 않는다."""
    drop(box, "하위/묶음 가_2030-01-07.pdf", "a_2030-01-07")
    drop(box, "x/scan.pdf", "b_2030-01-07")
    drop(box, "y/scan.pdf", "d_2030-01-08")
    drop(box, "_mine/c_2030-01-07.pdf", "c_2030-01-07")
    drop(box, ".hidden.pdf", "c_2030-01-07")
    drop(box, "~lock.pdf", "c_2030-01-07")
    drop(box, "notes.txt", box["files"]["c_2030-01-07"])
    r = Worker(box["pipe"], inbox_of(box)).run_once()
    assert len(r["received"]) == 3
    names = sorted(x[0] for x in box["pipe"].con.execute("SELECT source_name FROM doc_document"))
    assert names == ["scan", "scan", "묶음 가_2030-01-07"]
    assert visible(box) == [".hidden.pdf", "notes.txt", "~lock.pdf"]
    assert (box["st"].inbox / "_mine" / "c_2030-01-07.pdf").exists()
    assert r["needs_date"] and len(r["needs_date"]) == 2               # "scan" 에는 날짜가 없다


def test_nothing_is_deleted_every_byte_is_somewhere(box, monkeypatch):
    """한 바퀴 뒤 접수 폴더(_ 폴더 밖)가 비어 있고, 넣은 파일 전부가 보관 폴더·_already·_failed 중 한 곳에 바이트 그대로 있다.
    잠겨서 읽을 수 없는 파일은 그대로 두고 다시 보다가 give_up 뒤에 _failed 로."""
    put = {name: sha(drop(box, name, src)) for name, src in
           (("a_2030-01-07.pdf", "a_2030-01-07"), ("again.pdf", "a_2030-01-07"), ("u1_2030-01-07.pdf", "u1_2030-01-07"),
            ("locked_2030-01-07.pdf", "b_2030-01-07"))}
    real_open = open

    def locked(path, *a, **kw):
        if str(path).endswith("locked_2030-01-07.pdf"):
            raise PermissionError("다른 프로그램이 쓰고 있다")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(ib, "open", locked, raising=False)
    w = Worker(box["pipe"], inbox_of(box))
    r = w.run_once()
    assert len(r["received"]) == 2 and r["already"] == 1 and visible(box) == ["locked_2030-01-07.pdf"]
    box["clock"].t += 200
    assert w.run_once()["moved_failed"] == 1 and visible(box) == []
    where = everywhere(box)
    assert all(h in where for h in put.values())


def test_a_failed_move_is_retried_next_round(box, monkeypatch):
    """윈도우: 열려 있는 파일은 지울(옮길) 수 없다 — 다음 바퀴에 다시. 바이트는 그대로 남는다."""
    f = drop(box, "a_2030-01-07.pdf", "a_2030-01-07")
    real = Path.unlink
    calls = []

    def busy(self, *a, **kw):
        if self == f and not calls:
            calls.append(1)
            raise PermissionError("열려 있다")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", busy)
    w = Worker(box["pipe"], inbox_of(box))
    r = w.run_once()
    assert r["retry"] == 1 and f.exists()                           # 등록은 되었다 — 접수 폴더의 것만 남았다
    assert box["pipe"].con.execute("SELECT COUNT(*) FROM doc_document").fetchone()[0] == 1
    r = w.run_once()
    assert r["already"] == 1 and visible(box) == []
    assert len(list((box["st"].archive_root / "intake").rglob("*.pdf"))) == 1   # 보관 폴더에는 한 벌만


def test_interrupted_copy_and_interrupted_round_resume(box, monkeypatch):
    """보관 폴더로 복사하다 끊긴 임시 파일은 문서가 되지 않는다. 접수만 하고 끊은 뒤(received) 다시 돌리면 한 번에 돌린 것과 같다."""
    drop(box, "a_2030-01-07.pdf", "a_2030-01-07")
    drop(box, "b_2030-01-07.pdf", "b_2030-01-07")
    real = os.replace
    monkeypatch.setattr(ib.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("끊겼다")))
    inbox = inbox_of(box)
    assert inbox.round(box["pipe"])["retry"] == 2
    monkeypatch.setattr(ib.os, "replace", real)
    leftovers = list((box["st"].archive_root / "intake").rglob("*" + ib.TEMP_SUFFIX))
    probe = Pipeline(replace(box["st"], work_root=box["root"] / "probe"), site=box["site"])
    assert probe.expand([box["st"].archive_root]) == []               # .part 는 run 이 줍지 않는다
    assert all(p.suffix == ib.TEMP_SUFFIX for p in leftovers)
    r = inbox.round(box["pipe"])                                       # 접수만 (처리 전에 끊겼다)
    assert len(r["received"]) == 2
    assert {x[0] for x in box["pipe"].con.execute("SELECT status FROM doc_document")} == {"received"}
    again = Pipeline(box["st"], site=box["site"])                      # 다시 띄운 감시
    assert Worker(again, inbox_of(box)).run_once()["processed"] == 2
    ref_st = replace(box["st"], work_root=box["root"] / "ref", archive_root=box["root"] / "ref-보관",
                     inbox=box["root"] / "ref-스캐너")
    ref_st.archive_root.mkdir()
    ref_box = dict(box, st=replace(ref_st), pipe=Pipeline(ref_st, site=box["site"]))
    ref_box["st"].inbox.mkdir()
    drop(ref_box, "a_2030-01-07.pdf", "a_2030-01-07")
    drop(ref_box, "b_2030-01-07.pdf", "b_2030-01-07")
    Worker(ref_box["pipe"], inbox_of(ref_box)).run_once()
    skip = (*SKIP, "source_rel")
    assert_same(dump(again.con, skip), dump(ref_box["pipe"].con, skip), "끊긴 뒤 = 한 번에")


def test_rebuilding_from_the_archive_equals_the_intake_db(box):
    """DB 를 지우고 보관 폴더를 run 으로 다시 돌린 DB 가 접수로 만든 DB 와 같다 (문서의 순서·받은 시각이 폴더 이름에 있다)."""
    for name, src in (("b_2030-01-07.pdf", "b_2030-01-07"), ("a_2030-01-07.pdf", "a_2030-01-07"),
                      ("u1_2030-01-07.pdf", "u1_2030-01-07"), ("e_2030-01-07.pdf", "e_2030-01-07")):
        drop(box, name, src)
        box["clock"].t += 1
    w = Worker(box["pipe"], inbox_of(box))
    w.run_once()
    no_null_dates(box["pipe"].con)
    assert box["pipe"].con.execute("SELECT COUNT(*) FROM doc_page WHERE status = 'duplicate'").fetchone()[0] == 2
    fresh = Pipeline(replace(box["st"], work_root=box["root"] / "rebuilt"), site=box["site"])
    fresh.run([box["st"].archive_root])
    skip = tuple(c for c in SKIP if c != "received_at")                 # 받은 시각도 같아야 한다 (폴더 이름에서)
    assert_same(dump(box["pipe"].con, skip), dump(fresh.con, skip), "보관 폴더로 다시")


@pytest.mark.slow
def test_a_day_split_in_two_files_equals_one_file(box, rescan_synth):
    """하루치를 두 파일로 나눠 넣은 결과가 한 파일일 때와 같다 (쪽 번호가 달라지므로 내용으로 비교한다)."""
    first = sorted(rescan_synth.scans.glob("scan_*.pdf"))[0]
    tmp = box["root"] / "parts"
    one = split_pages(first, [1, 2, 3, 4, 5, 6], tmp / "one_2030-01-07.pdf")
    p1, p2 = split_pages(first, [1, 2, 3], tmp / "p1.pdf"), split_pages(first, [4, 5, 6], tmp / "p2.pdf")
    # 일보의 차량번호·작성자 라벨 (자리 배정이 돌게) — 라벨은 파일명#쪽으로 찾으므로 두 이름 벌에 다 적는다
    site = box["root"] / "site"
    shutil.copytree(rescan_synth.site, site)
    src = json.loads((rescan_synth.site / "labels" / "pages.json").read_text(encoding="utf-8"))
    labels = {}
    for k, v in src.items():
        n = int(k.rsplit("#", 1)[1])
        labels[f"one_2030-01-07#{n}"] = v
        labels[f"first_2030-01-07#{n}" if n <= 3 else f"second_2030-01-07#{n - 3}"] = v
    (site / "labels" / "pages.json").write_text(json.dumps(labels), encoding="utf-8")
    sp = SitePack(site)

    def content(files) -> dict:
        root = box["root"] / f"r{len(files)}"
        st = replace(box["st"], site=site, work_root=root / "work", archive_root=root / "보관", inbox=root / "in")
        st.archive_root.mkdir(parents=True)
        st.inbox.mkdir()
        b = dict(box, st=st, pipe=Pipeline(st, site=sp))
        for i, (name, src) in enumerate(files):
            drop(b, name, src, age=60 - i)
        Worker(b["pipe"], inbox_of(b)).run_once()
        con = b["pipe"].con
        out = {}
        for t in BUSINESS:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")
                    if not (r[1].endswith("_id") or r[1] in ("page_id", "other_page_id", "field_a", "field_b"))]
            out[t] = sorted((tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {t}")), key=repr)
        out["pages"] = sorted(tuple(r) for r in con.execute("SELECT template_name, status, work_date FROM doc_page"))
        return out

    a = content([("one_2030-01-07.pdf", one)])
    b = content([("first_2030-01-07.pdf", p1), ("second_2030-01-07.pdf", p2)])
    assert a == b and len(a["pages"]) == 6


def test_watch_refuses_an_inbox_inside_the_archive(box, monkeypatch):
    common = ["--site", str(box["st"].site), "--archive-root", str(box["st"].archive_root), "--work-root",
              str(box["st"].work_root)]
    monkeypatch.setenv("MINEDOCSCAN_INBOX", str(box["st"].archive_root / "scans"))
    with pytest.raises(SystemExit) as e:
        main(["watch", "--once", *common])
    assert "접수 폴더를 쓸 수 없습니다" in str(e.value) and "\n" not in str(e.value)
    with pytest.raises(ib.InboxError):
        ib.check_paths(box["st"].archive_root.parent, box["st"].archive_root, {})   # 보관 폴더를 품는다
    with pytest.raises(ib.InboxError):
        ib.check_paths(box["st"].inbox, None, {})                                   # archive_root 가 없다


def test_watch_once_command(box, capsys, monkeypatch):
    """watch --once --settle-seconds 0 --give-up-seconds 0: 접수하고 처리한다. 출력에 파일명이 없다. info 에 접수 폴더."""
    drop(box, "a_2030-01-07.pdf", "a_2030-01-07", age=0)
    monkeypatch.setenv("MINEDOCSCAN_INBOX", str(box["st"].inbox))
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(box["st"].reviews))
    common = ["--site", str(box["st"].site), "--archive-root", str(box["st"].archive_root), "--work-root",
              str(box["root"] / "cli-work")]
    assert main(["watch", "--once", "--settle-seconds", "0", "--give-up-seconds", "0", *common]) == 0
    out = capsys.readouterr().out
    assert "받은 문서 1건" in out and "a_2030-01-07" not in out
    assert main(["info", "--json", *common]) == 0
    assert json.loads(capsys.readouterr().out)["inbox"]["path"] == str(box["st"].inbox)
    assert visible(box) == []


def test_received_folder_names_follow_the_order_even_if_the_clock_goes_back(box):
    """한 바퀴 안에서는 (수정 시각, 이름) 순서로 접수하고, 받은 시각이 지금까지의 가장 늦은 것보다 늦지 않으면 1 ms 뒤로 민다."""
    drop(box, "z_2030-01-07.pdf", "a_2030-01-07", age=90)
    drop(box, "y_2030-01-07.pdf", "b_2030-01-07", age=80)
    inbox = inbox_of(box)
    inbox.round(box["pipe"])
    box["clock"].w = WALL - 3600                                        # 시계가 뒤로 갔다
    drop(box, "x_2030-01-07.pdf", "c_2030-01-07", age=70)
    inbox_of(box).round(box["pipe"])                                    # 새 감시 — DB 에서 가장 늦은 시각을 읽는다
    rows = box["pipe"].con.execute("SELECT source_name, source_rel FROM doc_document").fetchall()
    order = [r[0] for r in sorted(rows, key=lambda r: r[1])]
    assert order == ["z_2030-01-07", "y_2030-01-07", "x_2030-01-07"]
    assert ib.received_from_rel("intake/2030-01/20300101T000000001Z-0123456789abcdef/a.pdf") == "2030-01-01T00:00:00.001Z"
    assert ib.received_from_rel("scans/a.pdf") is None


def test_complete_checks_the_jpeg_end_marker(tmp_path):
    """OpenCV 4.9 는 잘린 JPEG 도 디코딩한다 — 끝 표시(FF D9)까지 있어야 다 쓰인 것이다."""
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".jpg", np.full((40, 60), 200, np.uint8))
    whole, cut = tmp_path / "w.jpg", tmp_path / "c.jpg"
    whole.write_bytes(buf.tobytes())
    cut.write_bytes(buf.tobytes()[:-40])
    assert ib.complete(whole) and not ib.complete(cut)


def test_complete_waits_for_the_pdf_end_marker(tmp_path):
    """끝이 덜 쓰인 PDF(마지막 1 KB 에 %%EOF 가 없다 — 열리더라도)는 "아직 쓰이는 중"이다 (tasks/0009 4.3 — 손상 방침 fail 과 같다)."""
    import numpy as np

    from minedocscan.tools.pdfwrite import write_images

    whole = write_images(tmp_path / "w.pdf", [np.full((300, 200), 220, np.uint8)])
    data = whole.read_bytes()
    noeof, cut = tmp_path / "n.pdf", tmp_path / "c.pdf"
    noeof.write_bytes(data.rstrip()[: -len(b"%%EOF")])
    cut.write_bytes(data[: len(data) // 2])
    assert ib.complete(whole) and not ib.complete(noeof) and not ib.complete(cut)


def test_a_name_whose_archive_path_is_too_long_stays_in_the_inbox(box):
    """보관 경로(intake/<해-달>/<받은 시각>-<문서 ID>/<원래 이름>, 쓰는 동안의 임시 이름까지)가 259자를 넘는 파일은 접수하지 않고
    접수 폴더에 그대로 두고 이유를 수로 알린다 — 요약(수가 바뀐 바퀴에만)과 홈. 이름을 줄이지 않는다 (보관한 이름이 source_name 이
    되고 날짜 규칙이 그것을 다시 읽는다). 긴 한글 이름 (tasks/0009 4.4) — 리눅스의 이름 한 칸은 255 바이트라 80자."""
    import sys

    from minedocscan.cli import _worth_showing
    from minedocscan.intake.worker import Worker, format_round
    from minedocscan.review.ops import OpsApp

    st = box["st"]
    long_name = "가동일보" * (27 if sys.platform == "win32" else 20) + "_2030-01-07.pdf"
    probe = inbox_of(box)
    extra = max(1, ib.PATH_MAX + 20 - len(probe._longest) - len(long_name + ib.TEMP_SUFFIX))
    archive = box["root"] / ("보관" + "x" * extra)                    # 보관 폴더를 그만큼 깊게 — 접수 폴더의 경로는 짧다
    archive.mkdir()
    box["st"] = st = replace(st, archive_root=archive)
    drop(box, long_name, "a_2030-01-07")
    drop(box, "u1_2030-01-07.pdf", "u1_2030-01-07")
    inbox = inbox_of(box)
    assert inbox.too_long(long_name) and not inbox.too_long("u1_2030-01-07.pdf")
    worker = Worker(box["pipe"], inbox)
    r = worker.run_once()
    assert r["too_long"] == 1 and len(r["received"]) == 1 and long_name in visible(box)
    assert "보관 경로가 너무 길어" in format_round(r) and _worth_showing(r)
    assert OpsApp(box["pipe"].con, box["site"], st, "jp", worker=worker).home_json()["todo"]["too_long"] == 1
    r = worker.run_once()                                              # 그대로 — 요약은 수가 바뀐 바퀴에만
    assert r["too_long"] == 1 and not r["too_long_changed"] and not _worth_showing(r)
    (st.inbox / long_name).rename(st.inbox / "a_2030-01-07.pdf")      # 사람이 이름을 줄였다
    r = worker.run_once()
    assert r["too_long"] == 0 and len(r["received"]) == 1 and visible(box) == []
