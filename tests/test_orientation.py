"""쪽의 방향과 빈 쪽 (tasks/0007 단계 1, 4.4·4.5).

돌아서 들어온 쪽은 첫 정합의 호모그래피에서 방향을 읽고 정확히(np.rot90) 세워 다시 정합한다 — 바로 선 쪽과 결과가 같아야 한다.
무손실(PNG)로 비교한다: 합성 PDF 는 쪽을 JPEG 로 담으므로 돌린 뒤에 담으면 화소가 달라진다.
쪽은 세션의 합성 묶음(synth — 점검표·운반, usage_synth — 가동 일보 두 종·동시 판·인쇄 층)에서 양식마다 한 쪽씩 꺼내 쓴다.
"""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from conftest import day_pdf, fast_imaging
from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.forms.template import Template
from minedocscan.imaging.align import (
    ROTATIONS,
    align_to_template,
    align_upright,
    orientation,
    rotate_upright,
    rotation_matrix,
    warp_to_template,
)
from minedocscan.imaging.cells import page_ink
from minedocscan.imaging.cropspec import CropSpec, PageImages, crop_cell
from minedocscan.imaging.io import imread_gray, imwrite, load_page
from minedocscan.pipeline.runner import Pipeline
from minedocscan.report import build_report, list_pages
from minedocscan.tools import synth_usage
from minedocscan.tools.synth import (
    BLANK_KINDS,
    T_INSP,
    T_LOG,
    T_MATRIX,
    _write_pdf,
    blank_back,
    rotate_scan,
    scan_effect,
)

DAY = "2030-01-07"
# 양식마다 한 쪽, 돌리는 각 (90·180·270 이 두 번씩). 동시 판 둘(A·B)과 인쇄 층이 있는 양식이 들어 있다
TURNS = {T_INSP: 90, T_LOG: 180, T_MATRIX: 270, synth_usage.T_LOADER: 90, synth_usage.T_USAGE: 180,
         synth_usage.T_USAGE_B: 270}
# 비교에서 빼는 열: 방향과 그것을 합성한 호모그래피 (정합 그림은 같다), 시각·경로. 분류는 들어온 그대로의 쪽으로 한다(4.4) —
# 분류 여유(1위/2위 인라이어 비율)는 돌아간 쪽의 특징점으로 잰 것이라 조금 다르다 (양식은 같다)
DROP = {"rotation", "homography", "classify_margin", "created_at", "received_at", "source_path", "source_rel", "aligned_image"}
TABLES = ("doc_document", "doc_page", "doc_field", "doc_page_meta", "prod_haul", "prod_tally", "eq_usage_daily",
          "insp_daily", "xcheck_haul", "eq_assignment_obs", "xcheck_usage", "eq_equipment")


def _first_pages(synth, usage_synth) -> dict[str, np.ndarray]:
    """양식 → 그 양식의 첫 쪽을 200 dpi 로 렌더링한 그림 (파이프라인이 PDF 에서 보는 그림과 같다)."""
    out = {}
    for s, pdf in ((synth, day_pdf(synth, 0)), (usage_synth, sorted(usage_synth.scans.glob("*.pdf"))[0])):
        for p in s.truth["documents"][pdf.stem]:
            if p["template"] in TURNS and p["template"] not in out:
                out[p["template"]] = load_page(pdf, p["page"], 200)
    assert set(out) == set(TURNS)
    return out


def _run(settings: Settings, folder) -> Pipeline:
    pipe = Pipeline(settings)
    pipe.run([folder])
    return pipe


def _settings(usage_synth, root, name: str, **kw) -> Settings:
    return Settings(site=usage_synth.site, archive_root=root, work_root=root / f"work_{name}", reviews=root / "none.jsonl",
                    **kw)


@pytest.fixture(scope="module")
def orient(synth, usage_synth, tmp_path_factory) -> dict:
    """같은 쪽들을 바로 선 PNG 문서(up/)와 정확히 돌린 PNG 문서(rot/ — TURNS 의 각만큼 반시계 방향)로 넣어 따로 돌린 두 DB.
    문서 이름은 두 폴더에서 같다 (scan_<날짜>_<양식>) — 라벨이 붙지 않는 것도 같다."""
    root = tmp_path_factory.mktemp("orient")
    pages = _first_pages(synth, usage_synth)
    for name, img in pages.items():
        imwrite(root / "up" / f"scan_{DAY}_{name}.png", img)
        imwrite(root / "rot" / f"scan_{DAY}_{name}.png", rotate_scan(img, TURNS[name]))
    with pytest.MonkeyPatch.context() as mp:                # 세운 쪽은 바로 선 쪽과 화소까지 같다 — 그 정합을 다시 쓴다 (시간)
        fast_imaging(mp)
        up = _run(_settings(usage_synth, root, "up", save_aligned=False), root / "up")
        rot = _run(_settings(usage_synth, root, "rot"), root / "rot")
    return {"root": root, "pages": pages, "up": up, "rot": rot}


def _dump(con) -> dict:
    """비교할 표들: 문서 ID 를 문서 이름으로 바꾸고(돌린 문서는 바이트가 달라 ID 가 다르다) DROP 의 열을 뺀 행들 (정렬)."""
    ids = {r[0]: r[1] for r in con.execute("SELECT document_id, source_name FROM doc_document")}

    def norm(v):
        if isinstance(v, str):
            for d, n in ids.items():
                v = v.replace(d, n)
        return v

    out = {}
    for t in TABLES:
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})") if c[1] not in DROP]
        out[t] = sorted((tuple(norm(r[c]) for c in cols) for r in con.execute(f"SELECT * FROM {t}")), key=repr)
    return out


# ── 수용 기준 1: 정확히 돌린 쪽의 결과 = 바로 선 쪽의 결과 ─────────────────────────────────
def test_exactly_rotated_pages_give_the_upright_result(orient):
    up, rot = orient["up"].con, orient["rot"].con
    a, b = _dump(up), _dump(rot)
    assert a["doc_field"] and a["prod_haul"] and a["eq_usage_daily"] and a["prod_tally"] and a["insp_daily"]
    for t in TABLES:
        assert a[t] == b[t], t
    turns = {r["source_name"].split("_", 2)[2]: r["rotation"] for r in rot.execute(
        "SELECT d.source_name, p.rotation FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id")}
    assert turns == TURNS
    assert {r[0] for r in up.execute("SELECT rotation FROM doc_page")} == {0}
    assert {r[0] for r in rot.execute("SELECT status FROM doc_page")} == {"loaded"}
    # 판이 둘인 운행일보는 세운 쪽으로 판을 다시 골랐다 — 판 B 쪽이 판 B 로
    tpl = {r[0]: r[1] for r in rot.execute("SELECT d.source_name, p.template_name FROM doc_page p "
                                           "JOIN doc_document d ON p.document_id = d.document_id")}
    assert tpl[f"scan_{DAY}_{synth_usage.T_USAGE_B}"] == synth_usage.T_USAGE_B
    # 리포트: 방향별 쪽 수 (0 이 아닌 것만), 바로 선 묶음에는 키가 없다
    assert build_report(rot)["intake"] == {"rotated": {"90": 2, "180": 2, "270": 2}}
    assert "intake" not in build_report(up)
    assert {r["page_id"] for r in list_pages(rot, rotated=True)} == {r[0] for r in rot.execute("SELECT page_id FROM doc_page")}
    assert list_pages(up, rotated=True) == []


def test_saved_homography_maps_the_original_page(orient):
    """저장한 호모그래피는 렌더링한 원래(돌아간) 쪽 → 템플릿: 그것으로 원래 쪽을 다시 편 그림이 정합 그림과 같다 (보간 자리만
    조금 다를 수 있다 — 4.4). 원본 해상도 크롭은 방향을 몰라도 바로 선 쪽의 크롭과 같은 칸을 보여 준다 (같은 크기, 평균 차 2 이하)."""
    root, up, rot = orient["root"], orient["up"].con, orient["rot"].con
    spec = CropSpec("source", 1.5, None)
    n = 0
    for name in TURNS:
        src_up, src_rot = (root / d / f"scan_{DAY}_{name}.png" for d in ("up", "rot"))
        rows = {}
        for con in (up, rot):
            rows[con] = con.execute("SELECT p.*, d.document_id FROM doc_page p JOIN doc_document d ON p.document_id = "
                                    "d.document_id WHERE d.source_name = ?", (f"scan_{DAY}_{name}",)).fetchone()
        tpl = orient["up"].site.templates[rows[up]["template_name"]]
        H = json.loads(rows[rot]["homography"])
        gray = imread_gray(src_rot)
        aligned = imread_gray(orient["rot"].settings.work_root / rows[rot]["aligned_image"])
        assert np.abs(warp_to_template(gray, H, tpl.reference.shape).astype(int) - aligned).mean() < 0.5, name
        imgs = {con: PageImages(source=src, homography=json.loads(rows[con]["homography"]), render_dpi=200, source_dpi=300)
                for con, src in ((up, src_up), (rot, src_rot))}
        for cell in [c for c in tpl.cells() if c.kind.startswith("handwritten")][:6]:
            a, b = (crop_cell(imgs[c], cell.bbox, spec) for c in (up, rot))
            assert a.shape == b.shape and np.abs(a.astype(int) - b).mean() <= 2, (name, cell.name)
            n += 1
    assert n >= 20


# ── 수용 기준 2: 돌린 쪽을 담은 PDF, 돌린 뒤 흔든 쪽 ─────────────────────────────────────
def test_rotated_pdf_and_rotated_then_shaken_pages_load_upright(orient, usage_synth, synth):
    """JPEG 로 담은 PDF(돌린 뒤에 담았다)와, 돌린 뒤 흔들기(scan_effect)를 건 쪽 — 정확한 90° 가 아니다 — 도 전부 loaded 이고 방향이
    맞게 읽힌다. 값 유무가 바로 선 PNG 문서와 달라진 칸의 수는 보고만 한다 (JPEG·흔들기로 조금은 다르다)."""
    root = orient["root"]
    rng = np.random.default_rng(70071)
    names = [T_LOG, synth_usage.T_LOADER, synth_usage.T_USAGE_B]           # 운반·작업량·동시 판 — 시간을 아껴 셋만
    pdf_pages, shaken = [], []
    for name in names:
        img = orient["pages"][name]
        pdf_pages.append(rotate_scan(img, TURNS[name]))
        shaken.append(scan_effect(rotate_scan(img, (TURNS[name] + 90) % 360), rng))
    _write_pdf(root / "pdf" / f"scan_{DAY}_rotated.pdf", pdf_pages)
    _write_pdf(root / "pdf" / "scan_2030-01-08_shaken.pdf", shaken)   # 다른 날 — 같은 날이면 같은 종이의 다시 스캔으로 붙잡힌다 (4.6)
    pipe = _run(_settings(usage_synth, root, "pdf"), root / "pdf")
    rows = pipe.con.execute("SELECT d.source_name, p.page_no, p.rotation, p.status, p.page_id FROM doc_page p "
                            "JOIN doc_document d ON p.document_id = d.document_id").fetchall()
    assert len(rows) == 2 * len(names) and {r["status"] for r in rows} == {"loaded"}
    want = {("rotated", i + 1): TURNS[n] for i, n in enumerate(names)} | \
           {("shaken", i + 1): (TURNS[n] + 90) % 360 for i, n in enumerate(names)}
    assert {(r["source_name"].rsplit("_", 1)[1], r["page_no"]): r["rotation"] for r in rows} == want
    # 값 유무가 달라진 칸 (보고용): 바로 선 PNG 문서의 같은 칸과
    up = {(r[0].split("_", 2)[2], r[1], r[2], r[3]): r[4] for r in orient["up"].con.execute(
        "SELECT d.source_name, f.region, f.field_name, f.row_no, f.has_value_raw FROM doc_field f JOIN doc_page p ON "
        "f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id")}
    diff = total = 0
    for r in pipe.con.execute("SELECT d.source_name, p.page_no, f.region, f.field_name, f.row_no, f.has_value_raw "
                              "FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d "
                              "ON p.document_id = d.document_id"):
        k = (names[r[1] - 1], r[2], r[3], r[4])
        total += 1
        diff += up.get(k) != r[5]
    assert total == 2 * sum(k[0] in names for k in up) and diff < 0.05 * total, (diff, total)


# ── 빈 쪽 ─────────────────────────────────────────────────────────────────────────────
def test_blank_backs_are_blank_and_do_not_need_review(usage_synth, tmp_path):
    """양면 스캔의 빈 뒷면(흰 종이·티·가장자리 그림자·옅게 비친 앞면)만 든 문서: 쪽은 전부 blank, 문서는 processed (blank 는
    문서를 needs_review 로 만들지 않는다). 손글씨가 없는 빈 양식은 blank 가 아니다 — 분류가 먼저다: 기준을 0.9 로 올려도
    양식을 찾은 쪽은 빈 쪽으로 보지 않는다."""
    rng = np.random.default_rng(70072)
    blank_form, _spec = synth_usage.build_usage_log()
    front = scan_effect(blank_form, rng)
    backs = [blank_back(front, rng, k) for k in BLANK_KINDS]
    assert max(page_ink(b) for b in backs) < 0.02 <= page_ink(front)
    _write_pdf(tmp_path / "in" / f"scan_{DAY}_backs.pdf", backs)
    imwrite(tmp_path / "in" / f"scan_{DAY}_form.png", front)
    pipe = _run(Settings(site=usage_synth.site, archive_root=tmp_path, work_root=tmp_path / "w",
                         reviews=tmp_path / "r.jsonl", blank_max_ink=0.9), tmp_path / "in")
    docs = {r[0].rsplit("_", 1)[1]: r[1] for r in pipe.con.execute("SELECT source_name, status FROM doc_document")}
    pages = {(r[0].rsplit("_", 1)[1], r[1]): (r[2], r[3]) for r in pipe.con.execute(
        "SELECT d.source_name, p.page_no, p.status, p.rotation FROM doc_page p JOIN doc_document d "
        "ON p.document_id = d.document_id")}
    assert docs["backs"] == "processed"
    assert all(pages[("backs", i + 1)] == ("blank", None) for i in range(len(BLANK_KINDS)))
    assert pages[("form", 1)] == ("loaded", 0)
    assert build_report(pipe.con)["intake"] == {"blank": len(BLANK_KINDS)}


# ── 함수 단위 ─────────────────────────────────────────────────────────────────────────
def test_orientation_reads_the_turn_and_rotation_matrix_moves_pixels():
    img, spec = synth_usage.build_loader_log()
    for rot in ROTATIONS:
        turned = rotate_scan(img, rot)                       # 반시계 방향으로 rot
        ar = align_to_template(turned, img, spec["regions"])
        assert orientation(ar.homography) == rot
        assert np.array_equal(rotate_upright(turned, rot), img)
        M = rotation_matrix(turned.shape, rot)
        ys, xs = np.nonzero(turned < 128)
        pts = M @ np.vstack([xs, ys, np.ones_like(xs)])
        assert np.array_equal(img[pts[1].astype(int), pts[0].astype(int)], turned[ys, xs])
    with pytest.raises(ValueError):
        rotate_upright(img, 45)


def test_align_upright_aligns_once_for_upright_pages_and_twice_for_turned():
    img, spec = synth_usage.build_usage_log()
    calls = []

    def align(g):
        calls.append(g.shape)
        return align_to_template(g, img, spec["regions"])

    ar, rot, up = align_upright(img, align)
    assert (rot, len(calls)) == (0, 1) and up is img
    calls.clear()
    ar2, rot2, up2 = align_upright(rotate_scan(img, 270), align)
    assert rot2 == 270 and len(calls) == 2 and np.array_equal(up2, img) and np.array_equal(ar2.warped, ar.warped)
    # 정합이 안 되면(인라이어 부족) 방향을 읽지 않는다 — 한 번만
    calls.clear()
    white = np.full_like(img, 255)
    _ar, rot3, _ = align_upright(white, align)
    assert rot3 == 0 and len(calls) == 1


# ── 도구: template init --rotate, preview --scan, print-layer 의 직접 정합 ─────────────────
def test_template_tools_upright_turned_scans(orient, usage_synth, tmp_path, capsys):
    from minedocscan.tools.mktemplate import init_template
    from minedocscan.tools.printlayer import page_image
    from minedocscan.tools.tpltools import preview

    root = orient["root"]
    name = synth_usage.T_LOADER
    up_png, rot_png = root / "up" / f"scan_{DAY}_{name}.png", root / "rot" / f"scan_{DAY}_{name}.png"
    # init --rotate: 돌아간 스캔으로 만든 템플릿의 기준 이미지가 세운 그림
    path = init_template(rot_png, "turned", tmp_path / "tpl", rotate=TURNS[name])
    assert np.array_equal(imread_gray(path.parent / "reference.png"), imread_gray(up_png))
    assert main(["template", "init", str(rot_png), "--name", "turned2", "--rotate", str(TURNS[name]), "--site",
                 str(tmp_path / "site2")]) == 0
    assert np.array_equal(imread_gray(tmp_path / "site2" / "templates" / "turned2" / "reference.png"), imread_gray(up_png))
    capsys.readouterr()
    # preview --scan: 돌아간 스캔도 세워서 정합해 그린다
    tdir = usage_synth.site / "templates" / name
    r = preview(tdir, tmp_path / "pv", scan=rot_png)
    assert r["aligned"]["ok"] and r["aligned"]["rotation"] == TURNS[name]
    # print-layer 의 직접 정합 (분류 전용 쪽): 세운 쪽의 정합 그림 = 바로 선 쪽의 것
    tpl = Template(tdir / "template.yaml")
    s = Settings(site=usage_synth.site, archive_root=root, work_root=tmp_path / "w")
    row = {"status": "classified_only", "aligned_image": None, "homography": None, "render_dpi": None, "source_rel": None,
           "page_no": 1}
    a, how_a = page_image({**row, "source_path": str(up_png)}, tpl, s, tpl.reference.shape)
    b, how_b = page_image({**row, "source_path": str(rot_png)}, tpl, s, tpl.reference.shape)
    assert how_a == how_b == "aligned_now" and np.array_equal(a, b)


def test_page_ink_counts_dark_pixels_after_opening():
    g = np.full((100, 100), 240, np.uint8)
    assert page_ink(g) == 0.0
    cv2.rectangle(g, (10, 10), (19, 19), 0, -1)            # 10×10 검은 네모 (이진화의 흐림으로 조금 넓어진다)
    assert 0.01 <= page_ink(g) <= 0.02
