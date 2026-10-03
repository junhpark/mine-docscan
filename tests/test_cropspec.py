"""인식기에 넘기는 크롭의 규격은 하나 (tasks/0003 단계 2).

같은 셀이면 export-crops 가 쓴 PNG 와 파이프라인이 인식기에 넘긴 배열이 화소까지 같아야 한다 — 원본(source)과
정합 이미지(aligned) 두 규격 모두. 그리고 [recognize.by_kind] 로 칸 종류마다 다른 백엔드가 받는다.
"""
import json
from dataclasses import replace

import cv2
import numpy as np
import pytest

from minedocscan.config import Settings
from minedocscan.imaging.cropspec import DEFAULT_SPEC, CropSpec, PageImages, crop_box, crop_cell
from minedocscan.pipeline import Pipeline
from minedocscan.recognize import (
    REGISTRY,
    ByKindRecognizer,
    Recognition,
    build_recognizer,
    register,
    spec_for,
)
from minedocscan.review.export import export_crops
from minedocscan.review.store import Review, save
from minedocscan.tools.synth import generate

SPEC_NUM = CropSpec("source", 1.5, None)          # 숫자 칸: 원본 해상도, 1.5배, 여유 = 행 높이의 절반
SPEC_TXT = CropSpec("aligned", 1.5, 3)            # 텍스트 칸: 정합 이미지, 1.5배, 여유 3 px


class Capture:
    """받은 크롭을 field_id 별로 모아 두는 가짜 백엔드."""

    seen: dict[str, dict] = {}

    def __init__(self, name: str, spec: CropSpec):
        self.name, self.crop_spec = name, spec
        Capture.seen.setdefault(name, {})

    def recognize(self, crops, contexts):
        for c, ctx in zip(crops, contexts, strict=True):
            Capture.seen[self.name][ctx.field_id] = (np.array(c, copy=True), ctx.kind)
        return [Recognition("", 0.0, [], self.name) for _ in contexts]


@pytest.fixture(scope="module")
def captured(tmp_path_factory):
    root = tmp_path_factory.mktemp("cropspec")
    synth = generate(root / "data", days=1, seed=0)
    register("fake_num", lambda: Capture("fake_num", SPEC_NUM))
    register("fake_txt", lambda: Capture("fake_txt", SPEC_TXT))
    Capture.seen = {}
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=root / "work", reviews=root / "r.jsonl",
                        recognizer="fake_txt", recognizer_by_kind={"handwritten_number": "fake_num"})
    try:
        pipe = Pipeline(settings)
        pipe.run([synth.scans])
    finally:
        REGISTRY.pop("fake_num", None)
        REGISTRY.pop("fake_txt", None)
    return {"synth": synth, "settings": settings, "pipe": pipe, "root": root, "seen": dict(Capture.seen)}


def test_by_kind_routes_each_kind_to_its_backend(captured):
    con, seen = captured["pipe"].con, captured["seen"]
    assert isinstance(captured["pipe"].recognizer, ByKindRecognizer)
    assert seen["fake_num"] and seen["fake_txt"]
    assert {k for _a, k in seen["fake_num"].values()} == {"handwritten_number"}
    assert {k for _a, k in seen["fake_txt"].values()} == {"handwritten_text"}
    by = {r[0]: r[1] for r in con.execute("SELECT kind || '/' || backend, COUNT(*) FROM doc_field "
                                          "WHERE backend LIKE 'fake_%' GROUP BY 1")}
    assert set(by) == {"handwritten_number/fake_num", "handwritten_text/fake_txt"}
    # 각 백엔드가 받은 크롭의 크기가 자기 규격대로다 (원본 1.5배, 여유 = 행 높이의 절반)
    fid, (arr, _k) = next(iter(seen["fake_num"].items()))
    x0, y0, x1, y1 = con.execute("SELECT x0, y0, x1, y1 FROM doc_field WHERE field_id=?", (fid,)).fetchone()
    p = max(8, (y1 - y0) // 2)
    assert arr.shape == (round((y1 - y0 + 2 * p) * 1.5), round((x1 - x0 + 2 * p) * 1.5))


@pytest.mark.parametrize("backend,spec,kind", [("fake_num", SPEC_NUM, "handwritten_number"),
                                               ("fake_txt", SPEC_TXT, "handwritten_text")])
def test_exported_png_equals_pipeline_crop(captured, backend, spec, kind, tmp_path):
    """export-crops 의 PNG == 파이프라인이 인식기에 넘긴 배열 (화소 단위)."""
    con, site, settings = captured["pipe"].con, captured["pipe"].site, captured["settings"]
    settings = replace(settings, reviews=tmp_path / "r.jsonl")
    fids = sorted(captured["seen"][backend])[:12]
    for i, fid in enumerate(fids):
        save(con, site, settings, Review(fid, "value" if i % 2 else "empty", "3" if i % 2 else "", "jp"))
    out = tmp_path / "crops"
    r = export_crops(con, site, settings, out, kind=kind, res=spec.res, out_scale=spec.scale, pad=spec.pad)
    assert r["written"] == len(fids) and set(r["by_source"]) == {spec.res}
    labels = [json.loads(x) for sp in ("test", "train") if (out / sp / "labels.jsonl").exists()
              for x in (out / sp / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {lab["field_id"] for lab in labels} == set(fids)
    for lab in labels:
        assert CropSpec.from_dict(lab["spec"]) == spec and lab["spec"]["res"] == lab["resolution"]
        png = cv2.imread(str(out / lab["file"]), cv2.IMREAD_GRAYSCALE)
        want = captured["seen"][backend][lab["field_id"]][0]
        assert png.shape == want.shape and np.array_equal(png, want), lab["field_id"]


def test_default_spec_is_the_old_crop():
    """규격을 선언하지 않은 백엔드는 지금과 같은 크롭(칸 그대로)을 받는다."""
    img = (np.arange(200 * 300) % 251).astype(np.uint8).reshape(200, 300)
    images = PageImages(aligned=img)
    bbox = (40, 30, 140, 60)
    assert np.array_equal(crop_cell(images, bbox, DEFAULT_SPEC), img[30:60, 40:140])
    assert spec_for(object(), "handwritten_number") == DEFAULT_SPEC
    # 그림 밖으로 나가는 상자는 흰색으로 채워 크기를 지킨다
    edge = crop_box(images, (-5, -5, 20, 10), 1.0, "aligned")
    assert edge.shape == (15, 25) and edge[0, 0] == 255 and edge[5, 5] == img[0, 0]


def test_crop_spec_validation_and_roundtrip():
    for bad in (dict(res="raw"), dict(scale=0), dict(scale=7), dict(pad=-1)):
        with pytest.raises(ValueError):
            CropSpec(**bad)
    s = CropSpec("source", 1.5, None)
    assert s.to_dict() == {"res": "source", "scale": 1.5, "pad": "auto"} and CropSpec.from_dict(s.to_dict()) == s
    assert CropSpec.from_dict({"res": "aligned", "scale": 1, "pad": 0}) == DEFAULT_SPEC


def test_build_recognizer_without_by_kind_is_the_plain_backend(tmp_path):
    r = build_recognizer(Settings(work_root=tmp_path))
    assert r.name == "null" and not isinstance(r, ByKindRecognizer)
    with pytest.raises(ValueError, match="by_kind"):
        build_recognizer(Settings(work_root=tmp_path, recognizer_by_kind={"checkmark": "null"}))
    with pytest.raises(KeyError):
        build_recognizer(Settings(work_root=tmp_path, recognizer="nope"))
