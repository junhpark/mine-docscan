"""전체 묶음 운용: 깨진 파일 격리, 쪽 오류 격리, --skip-existing, 쪽 목록·미리보기, 월별 진단."""
import json

import pytest

from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.handlers.haul import HaulHandler
from minedocscan.pipeline import Pipeline
from minedocscan.report import build_report, by_month, list_pages
from minedocscan.tools.pdfwrite import blank_page, copy_pages, read_pages, write_pdf
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
    pages = read_pages(pdf)
    write_pdf(root / "hard.pdf", [blank_page(pages[0].width_pt, pages[0].height_pt), *pages])     # 1쪽에 흰 종이
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


def test_page_error_is_isolated_and_blank_page_is_listed(hard_day, capsys):
    """끼워 넣은 흰 종이는 양식을 못 찾고 어두운 화소가 없으므로 blank (tasks/0007 4.5 — 예전 기대는 unknown_form).
    문서가 needs_review 인 것은 쪽 오류 때문이다 (blank 는 문서를 needs_review 로 만들지 않는다)."""
    con, settings, synth = hard_day["pipe"].con, hard_day["settings"], hard_day["synth"]
    pages = {r["page_no"]: r for r in list_pages(con)}
    assert pages[1]["status"] == "blank" and pages[3]["status"] == "error"
    assert "RuntimeError" in pages[3]["error"] and "시험용" in pages[3]["error"]
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE page_id=?", (pages[3]["page_id"],)).fetchone()[0] == 0
    assert all(r["status"] == "loaded" for n, r in pages.items() if n not in (1, 3))
    assert con.execute("SELECT COUNT(*) FROM doc_field WHERE page_id=?", (pages[2]["page_id"],)).fetchone()[0] > 0
    assert con.execute("SELECT status FROM doc_document").fetchone()[0] == "needs_review"
    assert hard_day["pipe"].summary["page_errors"] and hard_day["pipe"].summary["pages"] == len(pages)

    assert list_pages(con, status="unknown_form") == []
    blank = list_pages(con, status="blank")
    assert [r["page_no"] for r in blank] == [1]
    written = write_thumbs(settings, blank)
    assert len(written) == 1 and written[0].parent.name == "blank" and written[0].exists()
    assert written[0].name.endswith(f"-{blank[0]['document_id']}.png")         # 파일명이 겹쳐도 문서 ID 로 갈린다 (4.7)
    assert written[0].is_relative_to(settings.work_root)
    # 명령줄
    common = ["--site", str(synth.site), "--work-root", str(settings.work_root)]
    capsys.readouterr()
    assert main(["pages", "--status", "error", "--json"] + common) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["page_no"] for r in out["pages"]] == [3]
    assert main(["pages", "--status", "blank", "--thumbs", str(hard_day["root"] / "th")] + common) == 0
    assert "쪽 1개" in capsys.readouterr().out and (hard_day["root"] / "th" / "blank").exists()
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
    """동기화 중 잘린 PDF: 뒤쪽 쪽이 사라진 채 '양식 없음'으로 섞지 않고 failed 로 낸다 (PDFium 은 열지 않는다 — ADR 0023). 종료 코드 1, JSON 은 깨지지 않는다."""
    synth = generate(tmp_path / "data", days=1, seed=3)
    pdf = next(synth.scans.glob("*.pdf"))
    data = pdf.read_bytes()
    pdf.write_bytes(data[: len(data) // 2])
    common = ["--site", str(synth.site), "--archive-root", str(synth.scans), "--work-root", str(tmp_path / "work"), "--json"]
    capsys.readouterr()
    code = main(["run", "--fresh"] + common)
    out = json.loads(capsys.readouterr().out)                       # 라이브러리 메시지가 섞이면 여기서 깨진다
    assert code == 1 and out["report"]["documents_by_status"] == {"failed": 1}
    assert "열 수 없습니다" in out["run"]["failed"][0]["error"] and out["report"]["pages"] == 0
    pdf.write_bytes(data)
    assert main(["run", "--skip-existing"] + common) == 0
    assert json.loads(capsys.readouterr().out)["report"]["documents_by_status"] == {"needs_review": 1}


def test_damaged_pdf_policy_with_pdfium(tmp_path):
    """손상 방침 (tasks/0009 4.3 가): 열리지 않거나 마지막 1 KB 에 %%EOF 가 없으면 손상. 50 %·90 %·99.9 % 로 자른 합성 PDF 는 PDFium 이
    열지 않는다 — fail 도 warn 도 오류. 끝 표시만 없는 PDF 는 열린다 — fail 은 오류, warn 은 경고 한 줄과 쪽 전부. 합성 쪽의 크기는
    1654×2339 그대로 (page_px — PyMuPDF 와 같은 규칙. PDFium 의 render(scale) 는 봐주지 않는 올림이라 1655 가 된다)."""
    from minedocscan.imaging.io import DamagedPdfError, count_pages, load_page, load_pages

    synth = generate(tmp_path / "data", days=1, seed=3)
    pdf = next(synth.scans.glob("*.pdf"))
    data = pdf.read_bytes()
    n = count_pages(pdf)
    shapes = [img.shape for _, img in load_pages(pdf)]                # 세로 양식과 가로 양식(행렬) — 둘 다 A4 그대로
    assert set(shapes) == {(2339, 1654), (1654, 2339)} and load_page(pdf, n).shape == shapes[-1]
    for frac in (0.5, 0.9, 0.999):
        cut = tmp_path / f"cut{frac}.pdf"
        cut.write_bytes(data[: int(len(data) * frac)])
        for policy in ("fail", "warn"):
            with pytest.raises(DamagedPdfError, match="열 수 없습니다"):
                count_pages(cut, policy, [])
    noeof = tmp_path / "noeof.pdf"
    noeof.write_bytes(data.rstrip()[: -len(b"%%EOF")])
    with pytest.raises(DamagedPdfError, match="끝 표시"):
        count_pages(noeof, "fail")
    warnings: list[str] = []
    assert count_pages(noeof, "warn", warnings) == n and len(warnings) == 1 and "끝 표시" in warnings[0]
    noxref = tmp_path / "noxref.pdf"                                 # 객체는 다 있고 상호 참조표·끝만 잘렸다 — PDFium 은 다시 세워 연다
    noxref.write_bytes(data[: data.rindex(b"xref\n")])
    with pytest.raises(DamagedPdfError, match="끝 표시"):
        count_pages(noxref, "fail")
    assert count_pages(noxref, "warn", []) == n
    zero = tmp_path / "zero.pdf"                                     # 멀쩡한데 쪽이 없다 — "잘린 파일" 이 아니라 "쪽이 없습니다"
    body, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate((b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"), 1):
        offsets.append(len(body))
        body += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 3\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    zero.write_bytes(bytes(body) + b"trailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref)
    for policy in ("fail", "warn"):
        with pytest.raises(DamagedPdfError, match="쪽이 없습니다"):
            count_pages(zero, policy, [])
    late = tmp_path / "late.pdf"                                     # %%EOF 가 마지막 1 KB 밖 (뒤에 1 KB 넘는 쓰레기) — 손상으로 본다
    late.write_bytes(data + b" " * 1100)
    with pytest.raises(DamagedPdfError, match="끝 표시"):
        count_pages(late, "fail")


def test_damaged_pdf_warn_policy(synth, tmp_path, capsys, monkeypatch):
    """damaged_pdf = warn: 끝 표시(%%EOF)가 없어도 열리는 PDF 를 처리하고 경고를 남긴다(종료 코드 0). 열 수 없는 파일은 그래도 failed."""
    scans = tmp_path / "scans"
    scans.mkdir()
    first_scan = next(synth.scans.glob("*.pdf"))
    pdf = scans / f"one_page_{first_scan.stem[-10:]}.pdf"             # 날짜가 있는 이름 — 없으면 needs_date (tasks/0007 4.2)
    copy_pages([(first_scan, [1])], pdf)                               # 합성 문서의 첫 쪽만
    pdf.write_bytes(pdf.read_bytes().rstrip()[:-len(b"%%EOF")])        # 끝 표시만 없는 PDF (열린다)
    monkeypatch.setenv("MINEDOCSCAN_DAMAGED_PDF", "warn")
    common = ["--site", str(synth.site), "--archive-root", str(scans), "--work-root", str(tmp_path / "work"), "--json"]
    capsys.readouterr()
    code = main(["run", "--fresh"] + common)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and len(out["report"]["documents_by_status"]) == 1
    assert out["report"]["pages"] == 1 and out["report"]["documents"] == 1
    assert len(out["run"]["warnings"]) == 1 and "끝 표시" in out["run"]["warnings"][0]["warning"]
    assert out["report"]["warnings"] == {"n": 1, "documents": [pdf.stem]}
    assert main(["report"] + common[:-1]) == 0 and "경고 1건" in capsys.readouterr().out
    # 쓰레기 바이트와 0바이트 파일은 warn 에서도 failed
    first = out["report"]["documents_by_status"]
    (scans / "garbage.pdf").write_bytes(b"%PDF-1.4 broken " + b"\x00" * 500)
    (scans / "empty.pdf").write_bytes(b"")
    code = main(["run", "--skip-existing"] + common)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and sorted(d["source_name"] for d in out["run"]["failed"]) == ["empty", "garbage"]
    assert out["report"]["documents_by_status"] == {"failed": 2} | first
    # 잘못된 방침 값은 시작할 때 멈춘다 — 트레이스백 없이 한 줄 (tasks/0004 단계 1)
    monkeypatch.setenv("MINEDOCSCAN_DAMAGED_PDF", "ignore")
    with pytest.raises(SystemExit, match="설정 오류: .*damaged_pdf"):
        main(["run"] + common)


def test_by_month_prints_zero_not_dash():
    from minedocscan.report import format_by_month

    text = format_by_month([{"template": "t", "month": "2030-01", "pages": 1, "loaded": 1, "align_failed": 0, "error": 0,
                             "classified_only": 0, "min_inliers": 0, "grid_err_median": 0.0, "grid_err_max": 0.0,
                             "low_margin": 0}])
    assert "0.0" in text and " - " not in text.splitlines()[1]


def test_page_size_rule_matches_pymupdf():
    """쪽의 화소 = ceil(pt × dpi / 72 − 0.001) — PyMuPDF 로 잰 값 그대로 (반올림이 아니다: 1165.36 → 1166, 300 dpi A4 스캔 595.2 pt → 1654)."""
    from minedocscan.imaging.io import page_px

    measured = {595.44: 1654, 841.68: 2338, 595.2756: 1654, 841.8898: 2339, 595.0: 1653, 419.53: 1166, 612: 1700, 595.2: 1654,
                1000.001 * 72 / 200: 1000, 1000.0011 * 72 / 200: 1001}
    assert {pt: page_px(pt, 200) for pt in measured} == measured
