"""전체 묶음 운용: 깨진 파일 격리, 쪽 오류 격리, --skip-existing, 쪽 목록·미리보기, 월별 진단."""
import json

import pymupdf
import pytest

from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.handlers.haul import HaulHandler
from minedocscan.pipeline import Pipeline
from minedocscan.report import build_report, by_month, list_pages
from minedocscan.tools.synth import expected_xcheck, generate
from minedocscan.tools.thumbs import write_thumbs


@pytest.fixture(scope="module")
def broken(tmp_path_factory):
    """3일치 중 둘째 날 파일을 깨진 바이트로 바꾼 뒤 명령줄로 돌린다."""
    root = tmp_path_factory.mktemp("archive")
    synth = generate(root / "data", days=3, seed=0)
    pdfs = sorted(synth.scans.glob("*.pdf"))
    good = pdfs[1].read_bytes()
    pdfs[1].write_bytes(b"%PDF-1.4 broken " + b"\x00" * 500)
    common = ["--site", str(synth.site), "--archive-root", str(synth.scans), "--work-root", str(root / "work")]
    code = main(["run", "--fresh"] + common + ["--json"])
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=root / "work", reviews=root / "r.jsonl")
    return {"synth": synth, "root": root, "common": common, "code": code, "settings": settings,
            "broken_path": pdfs[1], "good_bytes": good}


def test_broken_file_is_isolated(broken, capsys):
    synth, settings = broken["synth"], broken["settings"]
    assert broken["code"] == 1                                             # 실패가 있으면 종료 코드 1
    pipe = Pipeline(settings)
    con = pipe.con
    docs = {r["source_name"]: r for r in con.execute("SELECT * FROM doc_document")}
    assert len(docs) == 3
    bad = docs[broken["broken_path"].stem]
    assert bad["status"] == "failed" and bad["error"] and "Traceback" not in bad["error"]
    assert con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id=?", (bad["document_id"],)).fetchone()[0] == 0
    for t in ("doc_field", "prod_haul"):
        assert con.execute(f"SELECT COUNT(*) FROM {t} x JOIN doc_page p ON x.page_id = p.page_id WHERE p.document_id=?",
                           (bad["document_id"],)).fetchone()[0] == 0
    # 나머지 이틀은 정답과 같다
    days = [d for d in synth.truth["days"] if d["date"] != bad["work_date"]]
    rep = build_report(con)
    assert rep["xcheck_haul"] == expected_xcheck(days, with_trips=False)
    assert rep["documents_by_status"] == {"failed": 1, "needs_review": 2}
    assert rep["pages"] == sum(len(v) for k, v in synth.truth["documents"].items() if k != bad["source_name"])
    assert all(r["source_rel"] == r["source_name"] + ".pdf" for r in docs.values())


def test_skip_existing_then_repair(broken):
    synth, settings = broken["synth"], broken["settings"]
    before = build_report(Pipeline(settings).con)
    pipe = Pipeline(settings)
    summary = pipe.run([synth.scans], skip_existing=True)
    assert summary["documents"] == 0 and summary["skipped"] == 2 and len(summary["failed"]) == 1   # failed 는 다시 한다
    assert build_report(pipe.con) == before
    # 깨졌던 파일을 고치면 그 파일만 처리된다
    broken["broken_path"].write_bytes(broken["good_bytes"])
    pipe = Pipeline(settings)
    summary = pipe.run([synth.scans], skip_existing=True)
    assert summary["documents"] == 1 and summary["skipped"] == 2 and summary["failed"] == []
    rep = build_report(pipe.con)
    assert rep["xcheck_haul"] == synth.truth["expected"]["xcheck_haul_has_only"]
    assert rep["documents_by_status"] == {"needs_review": 3}            # 같은 파일의 옛 실패 기록은 성공이 대체한다
    assert rep["pages"] == synth.truth["expected"]["pages"]
    # 월별 표의 쪽 수 합 = 전체 쪽 수
    rows = by_month(pipe.con)
    assert sum(r["pages"] for r in rows) == rep["pages"]
    assert {r["month"] for r in rows} == {"2030-01"} and all(r["loaded"] == r["pages"] for r in rows)


@pytest.fixture(scope="module")
def hard_day(tmp_path_factory):
    """하루치에 흰 종이 한 장을 끼워 넣고, 한 쪽에서 핸들러가 예외를 내게 한다."""
    root = tmp_path_factory.mktemp("hard_day")
    synth = generate(root / "data", days=1, seed=1)
    pdf = next(synth.scans.glob("*.pdf"))
    doc = pymupdf.open(str(pdf))
    doc.new_page(pno=0, width=doc[0].rect.width, height=doc[0].rect.height)       # 1쪽에 흰 종이
    doc.save(str(root / "hard.pdf"), no_new_id=True)
    doc.close()
    pdf.unlink()
    (root / "hard.pdf").rename(synth.scans / "scan_2030-01-07.pdf")

    orig = HaulHandler.load

    def boom(self, ctx):
        if ctx.page_no == 3:                                       # 흰 종이 뒤의 둘째 일보
            raise RuntimeError("시험용 예외 (값은 적지 않는다)")
        return orig(self, ctx)

    HaulHandler.load = boom
    try:
        settings = Settings(site=synth.site, archive_root=synth.scans, work_root=root / "work", reviews=root / "r.jsonl")
        pipe = Pipeline(settings)
        pipe.run([synth.scans])
        with pytest.raises(RuntimeError):
            Pipeline(settings.__class__(**{**settings.__dict__, "work_root": root / "work_strict"})).run([synth.scans], strict=True)
    finally:
        HaulHandler.load = orig
    return {"synth": synth, "root": root, "settings": settings, "pipe": pipe}


def test_page_error_is_isolated_and_unknown_form_is_listed(hard_day, capsys):
    con, settings, synth = hard_day["pipe"].con, hard_day["settings"], hard_day["synth"]
    pages = {r["page_no"]: r for r in list_pages(con)}
    assert pages[1]["status"] == "unknown_form" and pages[3]["status"] == "error"
    assert "RuntimeError" in pages[3]["error"] and "시험용" in pages[3]["error"]
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE page_id=?", (pages[3]["page_id"],)).fetchone()[0] == 0
    assert all(r["status"] == "loaded" for n, r in pages.items() if n not in (1, 3))
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE page_id=?", (pages[2]["page_id"],)).fetchone()[0] > 0
    assert con.execute("SELECT status FROM doc_document").fetchone()[0] == "needs_review"
    assert hard_day["pipe"].summary["page_errors"] and hard_day["pipe"].summary["pages"] == len(pages)

    unknown = list_pages(con, status="unknown_form")
    assert [r["page_no"] for r in unknown] == [1]
    written = write_thumbs(settings, unknown)
    assert len(written) == 1 and written[0].parent.name == "unknown_form" and written[0].exists()
    assert written[0].is_relative_to(settings.work_root)
    # 명령줄
    common = ["--site", str(synth.site), "--work-root", str(settings.work_root)]
    capsys.readouterr()
    assert main(["pages", "--status", "error", "--json"] + common) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["page_no"] for r in out["pages"]] == [3]
    assert main(["pages", "--status", "unknown_form", "--thumbs", str(hard_day["root"] / "th")] + common) == 0
    assert "쪽 1개" in capsys.readouterr().out and (hard_day["root"] / "th" / "unknown_form").exists()
    assert main(["report", "--by-month", "--json"] + common) == 0
    rows = json.loads(capsys.readouterr().out)["by_month"]
    assert sum(r["pages"] for r in rows) == len(pages) and sum(r["error"] for r in rows) == 1
    assert any(r["template"] == "unknown" and r["pages"] == 1 for r in rows)
    assert main(["report", "--by-month"] + common) == 0 and "unknown" in capsys.readouterr().out


def test_page_error_document_is_reprocessed_by_skip_existing(hard_day):
    """핸들러가 고쳐진 뒤 --skip-existing 으로 다시 돌리면 쪽 오류가 있던 문서는 건너뛰지 않고 다시 처리된다."""
    pipe = Pipeline(hard_day["settings"])
    summary = pipe.run([hard_day["synth"].scans], skip_existing=True)
    assert summary["documents"] == 1 and summary["skipped"] == 0 and summary["page_errors"] == []
    assert pipe.con.execute("SELECT COUNT(*) FROM doc_page WHERE status='error'").fetchone()[0] == 0
    summary = Pipeline(hard_day["settings"]).run([hard_day["synth"].scans], skip_existing=True)
    assert summary["documents"] == 0 and summary["skipped"] == 1


def test_truncated_pdf_is_failed_not_silently_repaired(tmp_path, capsys):
    """동기화 중 잘린 PDF: 라이브러리가 조용히 복구해 뒤쪽 쪽을 '양식 없음'으로 섞지 않고 failed 로 낸다. 종료 코드 1, JSON 은 깨지지 않는다."""
    synth = generate(tmp_path / "data", days=1, seed=3)
    pdf = next(synth.scans.glob("*.pdf"))
    data = pdf.read_bytes()
    pdf.write_bytes(data[: len(data) // 2])
    common = ["--site", str(synth.site), "--archive-root", str(synth.scans), "--work-root", str(tmp_path / "work"), "--json"]
    capsys.readouterr()
    code = main(["run", "--fresh"] + common)
    out = json.loads(capsys.readouterr().out)                       # 라이브러리 메시지가 섞이면 여기서 깨진다
    assert code == 1 and out["report"]["documents_by_status"] == {"failed": 1}
    assert "복구" in out["run"]["failed"][0]["error"] and out["report"]["pages"] == 0
    pdf.write_bytes(data)
    assert main(["run", "--skip-existing"] + common) == 0
    assert json.loads(capsys.readouterr().out)["report"]["documents_by_status"] == {"needs_review": 1}


def test_damaged_pdf_warn_policy(tmp_path, capsys, monkeypatch):
    """damaged_pdf = warn: 복구해서 연 PDF 를 처리하고 경고를 남긴다(종료 코드 0). 열 수 없는 파일은 그래도 failed."""
    synth = generate(tmp_path / "data", days=1, seed=3)
    pdf = next(synth.scans.glob("*.pdf"))
    data = pdf.read_bytes()
    pdf.write_bytes(data[:-200])                                    # 끝을 자른 PDF
    monkeypatch.setenv("MINEDOCSCAN_DAMAGED_PDF", "warn")
    common = ["--site", str(synth.site), "--archive-root", str(synth.scans), "--work-root", str(tmp_path / "work"), "--json"]
    capsys.readouterr()
    code = main(["run", "--fresh"] + common)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["report"]["documents_by_status"] == {"needs_review": 1}
    assert out["report"]["pages"] == synth.truth["expected"]["pages"]
    assert len(out["run"]["warnings"]) == 1 and "복구" in out["run"]["warnings"][0]["warning"]
    assert out["report"]["warnings"] == {"n": 1, "documents": [pdf.stem]}
    assert main(["report"] + common[:-1]) == 0 and "경고 1건" in capsys.readouterr().out
    # 쓰레기 바이트와 0바이트 파일은 warn 에서도 failed
    (synth.scans / "garbage.pdf").write_bytes(b"%PDF-1.4 broken " + b"\x00" * 500)
    (synth.scans / "empty.pdf").write_bytes(b"")
    code = main(["run", "--skip-existing"] + common)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and sorted(d["source_name"] for d in out["run"]["failed"]) == ["empty", "garbage"]
    assert out["report"]["documents_by_status"] == {"failed": 2, "needs_review": 1}
    # 잘못된 방침 값은 시작할 때 멈춘다
    monkeypatch.setenv("MINEDOCSCAN_DAMAGED_PDF", "ignore")
    with pytest.raises(ValueError, match="damaged_pdf"):
        main(["run"] + common)


def test_by_month_prints_zero_not_dash():
    from minedocscan.report import format_by_month

    text = format_by_month([{"template": "t", "month": "2030-01", "pages": 1, "loaded": 1, "align_failed": 0, "error": 0,
                             "classified_only": 0, "min_inliers": 0, "grid_err_median": 0.0, "grid_err_max": 0.0,
                             "low_margin": 0}])
    assert "0.0" in text and " - " not in text.splitlines()[1]
