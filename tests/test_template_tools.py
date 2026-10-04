"""템플릿 도구 (tasks/0005 단계 6): template preview·check, 합성 가동 일보의 손글씨가 OpenCV 판과 무관한지."""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from minedocscan.cli import main
from minedocscan.forms.template import Template, TemplateError
from minedocscan.tools import synth_cells, synth_meta, synth_usage
from minedocscan.tools.synth import write_site_pack
from minedocscan.tools.tpltools import COLORS, check_template, preview


@pytest.fixture(scope="module")
def pack(tmp_path_factory) -> Path:
    """합성 사이트 팩 (템플릿 다섯 종 — 기본 셋 + 가동 일보 둘). 쪽은 만들지 않는다."""
    return write_site_pack(tmp_path_factory.mktemp("tpl") / "site", usage=True)


def test_every_synthetic_template_checks_clean(pack):
    names = sorted(p.name for p in (pack / "templates").iterdir())
    assert names == ["synth_haul_log", "synth_haul_matrix", "synth_inspection", synth_usage.T_LOADER, synth_usage.T_USAGE]
    for n in names:
        assert check_template(pack / "templates" / n) == [], n


def test_preview_draws_one_box_per_cell_and_field(pack, tmp_path):
    for n in (synth_usage.T_LOADER, synth_usage.T_USAGE, "synth_inspection"):
        tpl = Template(pack / "templates" / n / "template.yaml")
        r = preview(pack / "templates" / n, tmp_path / "prev")
        cells = tpl.cells() + tpl.field_cells()
        assert r["boxes"] == len(cells) and r["aligned"] is None
        img = cv2.imread(r["out"])
        assert img.shape[:2] == tpl.reference.shape
        for c in cells:                                     # 칸마다 그 종류의 색 테두리가 있다 (오른쪽 아래 꼭짓점)
            x1, y1 = c.bbox[2], c.bbox[3]
            assert tuple(int(v) for v in img[y1, x1]) == COLORS[c.kind], (n, c.region, c.name, c.row)


def test_preview_refuses_the_repository(pack):
    with pytest.raises(ValueError, match="git 작업 트리"):
        preview(pack / "templates" / synth_usage.T_USAGE, Path(__file__).parent / "template-preview-should-not-exist")
    assert not (Path(__file__).parent / "template-preview-should-not-exist").exists()


def _broken(pack, tmp_path) -> Path:
    """일부러 망가뜨린 가동 일보 템플릿 — 오류 여덟 가지."""
    src = pack / "templates" / synth_usage.T_LOADER
    d = tmp_path / "broken"
    d.mkdir()
    (d / "reference.png").write_bytes((src / "reference.png").read_bytes())
    spec = yaml.safe_load((src / "template.yaml").read_text(encoding="utf-8"))
    regs = {r["name"]: r for r in spec["regions"]}
    regs["meter"]["columns"] = [c for c in regs["meter"]["columns"] if c["name"] != "end"]          # 1 역할에 필요한 칸
    regs["shifts"]["columns"][0]["format"] = "time"                                                # 2 인쇄된 칸에 형식
    regs["tally"]["columns"][2]["format"] = "hours"                                                # 3 모르는 형식
    regs["tally"]["columns"][4]["subtotal"] = "yes"                                                # 4 소계는 true/false
    regs["tally"]["columns"][3]["name"] = "a"                                                      # 5 열 이름 겹침
    fields = {f["name"]: f for f in spec["fields"]}
    fields["fuel"]["bbox"] = [2200, 855, 2500, 915]                                                # 6 쪽 밖 (폭 2339)
    fields["check"]["bbox"] = [1210, 710, 1500, 760]                                               # 7 계기 칸과 겹침
    spec["regions"].append(dict(regs["meter"], name="meter_b"))                                   # 8 계기 표가 둘
    (d / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return d


def test_check_lists_every_problem_of_a_broken_template(pack, tmp_path, capsys):
    d = _broken(pack, tmp_path)
    errs = check_template(d)
    want = ["role meter 에 필요한 칸이 없습니다: end", "shifts/shift: format 은 손으로 쓰는 칸", "알 수 없는 format 'hours'",
            "subtotal 은 true/false", "열 이름 'a' 이 겹칩니다", "role meter 인 표가 2개", "fields/fuel: 쪽 밖",
            "fields/check"]
    for w in want:                                                          # 여덟 가지를 한 번에
        assert any(w in e for e in errs), (w, errs)
    assert any("칸이 겹칩니다" in e and "fields/check" in e for e in errs)
    with pytest.raises(TemplateError) as e:                                 # 읽을 때는 첫 오류에서 멈춘다
        Template(d / "template.yaml")
    assert str(e.value) == errs[0]
    # 고칠 수 있는 것을 고치면 기하 오류 둘만 남는다
    spec = yaml.safe_load((d / "template.yaml").read_text(encoding="utf-8"))
    spec["regions"] = [r for r in spec["regions"] if r["name"] != "meter_b"]
    regs = {r["name"]: r for r in spec["regions"]}
    regs["meter"]["columns"].insert(1, {"idx": 1, "name": "end", "kind": "handwritten_number", "format": "reading"})
    regs["shifts"]["columns"][0].pop("format")
    regs["tally"]["columns"][2].pop("format")
    regs["tally"]["columns"][4]["subtotal"] = True
    regs["tally"]["columns"][3]["name"] = "ot"
    (d / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    errs = check_template(d)
    assert any("fields/fuel: 쪽 밖" in e for e in errs) and any("칸이 겹칩니다" in e and "fields/check" in e for e in errs)
    assert len(errs) == 2, errs
    # 명령: 오류가 있으면 목록과 종료 코드 1
    assert main(["template", "check", str(d)]) == 1
    out = capsys.readouterr().out
    assert "오류 2개" in out and out.count("\n- ") + out.startswith("- ") == 2
    (d / "template.yaml").write_text("name: [broken\n", encoding="utf-8")
    assert check_template(d)[0].startswith("YAML 을 읽을 수 없습니다")


def test_check_does_not_print_row_keys(tmp_path):
    """행 키(점검표에서는 장비 번호)가 겹쳐도 값은 찍지 않는다 — 행 번호만."""
    _img, spec = synth_usage.build_usage_log()
    spec["regions"][0]["rows"][1]["key"] = spec["regions"][0]["rows"][0]["key"] = "EQ-0101"
    d = tmp_path / "t"
    d.mkdir()
    cv2.imwrite(str(d / "reference.png"), _img)
    (d / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    errs = check_template(d)
    assert any("행 1 의 행 키가 앞의 행과 겹칩니다" in e for e in errs) and not any("EQ-0101" in e for e in errs)


# ── 합성 가동 일보의 손글씨는 OpenCV 판과 무관하다 ─────────────────────────────
def _fingerprint() -> np.ndarray:
    """정해진 씨앗의 가동 일보 손글씨 다섯 개(계기 값, 시각 범위 두 가지, 장비명, 작업 낱말)를 8×48 로 줄인 것."""
    out = []
    style = synth_meta.page_style(synth_meta.writer_style("ALPHA"), np.random.default_rng(0))
    for seed, text in ((1, "1234.5"), (2, "08:00~12:00"), (3, "8-12")):
        ink, _m, _w = synth_usage._text_ink(text, 34.0, style, np.random.default_rng(seed))
        out.append(ink)
    out.append(synth_meta.render_field_ink("LOADER", "name", 430, 80, 12, 1.0, style, np.random.default_rng(4)))
    canvas = np.zeros((90, 400), np.float32)
    synth_cells._draw_text_line(canvas, "stopped for water", 10, 45, 32, np.random.default_rng(5), style)
    out.append(canvas)
    return np.array([np.round(cv2.resize(x, (48, 8), interpolation=cv2.INTER_AREA) * 255).astype(int) for x in out])


def test_synthetic_usage_handwriting_does_not_depend_on_the_opencv_version():
    """같은 씨앗의 합성 가동 일보 글씨가 OpenCV 판과 무관하게 거의 같다 (tasks/0005 단계 6 — 0003·0004 와 같은 기준).
    기준은 OpenCV 5.0 에서 만든 것, 하한 판(CI lowest, 4.9)에서도 같은 시험이 돈다."""
    ref = np.array(json.loads((Path(__file__).parent / "fixtures" / "synth_usage_fingerprint.json").read_text()))
    got = _fingerprint()
    assert got.shape == ref.shape
    assert np.abs(got - ref).max() <= 3 and np.abs(got - ref).mean() < 0.5
    assert (_fingerprint() == got).all()
    assert (got.sum(axis=(1, 2)) > 0).all()                 # 다섯 개 다 잉크가 있다 (기호도 그려졌다)


def test_preview_on_an_aligned_scan(usage_synth, tmp_path):
    """--scan: 그 쪽을 정합해서 그 위에 그린다 — 칸이 실제 글씨에 맞는지 본다."""
    doc = sorted(usage_synth.truth["documents"])[0]
    page = next(p["page"] for p in usage_synth.truth["documents"][doc] if p["template"] == synth_usage.T_USAGE)
    r = preview(usage_synth.site / "templates" / synth_usage.T_USAGE, tmp_path / "p", scan=usage_synth.scans / f"{doc}.pdf",
                page=page)
    assert r["aligned"]["ok"] and r["aligned"]["grid_err"] <= 2 and Path(r["out"]).name.endswith(f"_p{page}.png")
