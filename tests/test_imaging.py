"""기하 단계(괘선 검출·정합·분류·체크 판정·글씨 덩어리 배정)를 합성 양식으로 시험한다."""
import numpy as np
import pytest

from minedocscan.forms.classify import FormClassifier
from minedocscan.imaging.align import align_to_template
from minedocscan.imaging.blobs import assign_blobs
from minedocscan.imaging.cells import observe_cells
from minedocscan.imaging.grid import detect_grid_roi
from minedocscan.imaging.io import imread_gray, imwrite, load_pages
from minedocscan.imaging.marks import decide_mark_pairs
from minedocscan.tools import synth as S


def _rng(seed=1):
    return np.random.default_rng(seed)


def test_unicode_path_roundtrip(tmp_path):
    img = np.full((40, 60), 200, np.uint8)
    p = tmp_path / "한글 폴더" / "일일장비 점검현황-1.png"
    imwrite(p, img)
    assert np.array_equal(imread_gray(p), img)
    assert [(n, g.shape) for n, g in load_pages(p)] == [(1, (40, 60))]
    with pytest.raises(ValueError):
        list(load_pages(tmp_path / "a.docx"))


@pytest.mark.parametrize("name", [S.T_INSP, S.T_LOG, S.T_MATRIX])
def test_grid_detection_finds_template_rules(site, name):
    tpl = site.templates[name]
    for reg in tpl.regions:
        gy, gx = reg["grid"]["ys"], reg["grid"]["xs"]
        ys, xs = detect_grid_roi(tpl.reference, (min(gx) - 12, min(gy) - 12, max(gx) + 12, max(gy) + 12))
        assert len(ys) == len(gy) and len(xs) == len(gx)
        assert max(abs(a - b) for a, b in zip(ys, gy, strict=True)) <= 2
        assert max(abs(a - b) for a, b in zip(xs, gx, strict=True)) <= 2


@pytest.mark.parametrize("name", [S.T_INSP, S.T_LOG, S.T_MATRIX])
def test_align_recovers_scanned_page(site, name):
    tpl = site.templates[name]
    scanned = S.scan_effect(tpl.reference, _rng(), strength=1.5)
    r = align_to_template(scanned, tpl.reference, tpl.regions, ref_features=tpl.features)
    assert r.ok and r.n_inliers >= 200 and r.grid_err_px <= 3.0


def test_align_rejects_other_form(site):
    insp, log = site.templates[S.T_INSP], site.templates[S.T_LOG]
    r = align_to_template(S.scan_effect(insp.reference, _rng()), log.reference, log.regions, ref_features=log.features)
    assert not r.ok


def test_classifier(site):
    clf = FormClassifier(list(site.templates.values()))
    for name, tpl in site.templates.items():
        res = clf.classify(S.scan_effect(tpl.reference, _rng(3)))
        assert res.template == name and res.margin >= 3.0
    assert clf.classify(np.full((2339, 1654), 255, np.uint8)).template is None       # 빈 종이


def _inspection_page(site, rows):
    """rows: {행 번호: 'yes' | 'yes_cross' | 'no'} 대로 ✓ 를 그린 (스캔 효과 없는) 점검표."""
    tpl = site.templates[S.T_INSP]
    img = tpl.reference.copy()
    reg = tpl.region("main")
    ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
    rng = _rng(5)
    for row, how in rows.items():
        col = 5 if how == "no" else 4
        S._tick(img, S._cell_bbox(ys, xs, row + 1, col), rng, cross_right=how == "yes_cross")
    return tpl, img


def test_checkmark_rule(site):
    n = len(S.EQUIPMENT)
    marks = {i: ("yes", "yes_cross", "no")[i % 3] for i in range(n - 2)}       # 마지막 두 행은 비워 둔다
    tpl, img = _inspection_page(site, marks)
    got = decide_mark_pairs(img, observe_cells(img, tpl), "abnormal_yes", "abnormal_no")
    for row, how in marks.items():
        # 경계선을 넘어 오른쪽 칸까지 그은 ✓ 도 왼쪽 칸(유)으로 읽어야 한다
        assert got[row].choice == ("second" if how == "no" else "first"), (row, how)
        assert got[row].status == "ok"
    assert got[n - 1].choice is None and got[n - 1].status == "empty"


def test_checkmark_column_unused(site):
    tpl, img = _inspection_page(site, {0: "no"})                                # 13행 중 1행만 표시 → 안 쓴 날
    got = decide_mark_pairs(img, observe_cells(img, tpl), "abnormal_yes", "abnormal_no")
    assert {m.status for m in got.values()} == {"column_unused"}
    assert all(m.choice is None for m in got.values())


def test_blob_assignment_ignores_notes(site):
    tpl = site.templates[S.T_MATRIX]
    reg = tpl.region("matrix")
    ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
    img = tpl.reference.copy()
    rng = _rng(7)
    filled = {(0, 2): "7", (3, 4): "12", (6, 5): "1"}                            # (데이터 행, 열 idx) → 값
    for (row, col), text in filled.items():
        S._hand_in_cell(img, text, S._cell_bbox(ys, xs, row + 2, col), rng, 1.5, 3, center=True)
    S._hand(img, "closed for blasting", xs[2] + 30, ys[11 + 2 + 1] - 22, rng, 1.5, 3)      # 11행: 여러 칸에 걸친 메모
    cells = [c for c in tpl.cells() if c.kind == "handwritten_number"]
    ink, blobs = assign_blobs(img, cells, ys, xs)
    got = {(cells[i].row, cells[i].col) for i, a in ink.items() if a >= 40}
    assert got == set(filled)
    assert any(b.is_note for b in blobs)
