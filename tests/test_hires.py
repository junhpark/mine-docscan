"""원본 해상도 크롭: 같은 셀을 더 촘촘히 뜬 것. 좌표계는 그대로 템플릿 좌표 하나다."""
import json

import cv2
import numpy as np

from minedocscan.review.crops import cell_crop, crop_region, field_info, row_crop


def _field(con):
    return con.execute("SELECT f.field_id FROM doc_field f JOIN prod_haul h ON h.source_field_id = f.field_id "
                       "WHERE h.has_value = 1 ORDER BY f.field_id LIMIT 1").fetchone()[0]


def test_source_crop_lands_on_the_same_place_as_aligned(null_run):
    con, settings = null_run.con, null_run.settings
    r = field_info(con, _field(con))
    assert r["homography"] and r["render_dpi"] == settings.dpi and len(json.loads(r["homography"])) == 3
    bbox = (r["x0"], r["y0"], r["x1"], r["y1"])
    src, where = crop_region(settings, r, bbox, 1.0, res="source")
    assert where == "source" and src.shape == (bbox[3] - bbox[1], bbox[2] - bbox[0])
    # 정합 이미지에서 사방 4 px 넓게 자른 뒤 원본 크롭을 그 안에서 찾는다: 상관 0.9 이상, 어긋남 1 px 이하
    search, where2 = crop_region(settings, r, (bbox[0] - 4, bbox[1] - 4, bbox[2] + 4, bbox[3] + 4), 1.0, res="aligned")
    assert where2 == "aligned"
    ncc = cv2.matchTemplate(search, src, cv2.TM_CCOEFF_NORMED)
    _, best, _, loc = cv2.minMaxLoc(ncc)
    assert best >= 0.9, best
    assert abs(loc[0] - 4) <= 1 and abs(loc[1] - 4) <= 1, loc
    # 배율
    big, _ = crop_region(settings, r, bbox, 1.5, res="source")
    assert big.shape == (round(src.shape[0] * 1.5), round(src.shape[1] * 1.5))
    # auto 는 원본이 닿으면 원본
    png, where3 = cell_crop(con, settings, r["field_id"], scale=1)
    assert where3 == "source" and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert row_crop(con, settings, r["field_id"])[1] == "source"


def test_falls_back_to_aligned_when_source_is_gone(null_run, synth):
    con, settings = null_run.con, null_run.settings
    fid = _field(con)
    r = field_info(con, fid)
    pdf = next(p for p in synth.scans.glob("*.pdf") if p.name == r["source_rel"])
    hidden = pdf.with_suffix(".hidden")
    pdf.rename(hidden)
    try:
        png, where = cell_crop(con, settings, fid, scale=2)
        assert where == "aligned" and png[:4] == b"\x89PNG"
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
        pad = max(8, (r["y1"] - r["y0"]) // 2)
        assert img.shape == ((r["y1"] - r["y0"] + 2 * pad) * 2, (r["x1"] - r["x0"] + 2 * pad) * 2)
    finally:
        hidden.rename(pdf)
    assert cell_crop(con, settings, fid, scale=1)[1] == "source"


def test_homography_is_stored_at_full_precision():
    """원근 항(1e-7 크기)이 반올림으로 0 이 되면 쪽의 구석에서 몇 px 어긋난다 (실데이터 최대 3.6 px)."""
    from minedocscan.pipeline.runner import homography_json

    h = np.array([[1.0012, -0.0031, 12.3456789], [0.0029, 0.9987, -7.6543219], [2.5e-7, -3.1e-7, 1.0]])
    back = np.array(json.loads(homography_json(h)))
    assert np.array_equal(back, h)


def test_load_page_renders_only_that_page(synth):
    from minedocscan.imaging.io import load_page, load_pages

    pdf = sorted(synth.scans.glob("*.pdf"))[0]
    pages = dict(load_pages(pdf, 50))
    assert np.array_equal(load_page(pdf, 3, 50), pages[3]) and np.array_equal(load_page(pdf, len(pages), 50), pages[len(pages)])
    import pytest

    with pytest.raises(KeyError):
        load_page(pdf, len(pages) + 1, 50)
