import json

import pytest
import yaml

from minedocscan.forms.sitepack import SitePack
from minedocscan.forms.template import Template, TemplateError
from minedocscan.tools.synth import EQUIPMENT, SLOTS, T_INSP, T_LOG, T_MATRIX, UG_ROWS


def test_site_pack_loads_three_templates(site):
    assert set(site.templates) == {T_INSP, T_LOG, T_MATRIX}
    assert site.name == "synthetic"
    assert site.iso_type("Loader") == "Loader" and site.iso_type("Charger") is None
    assert site.option("crosscheck.haul", "exclude_materials") == ["SURFACE"]
    assert site.option("no.such", "key", 7) == 7


def test_cells_follow_the_grid(site):
    tpl = site.templates[T_INSP]
    cells = tpl.cells()
    assert len(cells) == len(EQUIPMENT) * 6
    reg = tpl.region("main")
    first = next(c for c in cells if c.row == 0 and c.name == "remark")
    assert first.bbox == (reg["grid"]["xs"][3] + 4, reg["grid"]["ys"][1] + 4,
                          reg["grid"]["xs"][4] - 4, reg["grid"]["ys"][2] - 4)       # 머리글 한 줄 아래, 안쪽 여백 4
    assert first.row_key == EQUIPMENT[0][2]
    matrix = site.templates[T_MATRIX]
    slots = {c.col_meta["slot"] for c in matrix.cells() if c.kind == "handwritten_number"}
    assert slots == {s for s, _v, _o in SLOTS}
    assert len([c for c in matrix.cells() if c.kind == "handwritten_number"]) == len(SLOTS) * len(UG_ROWS)
    assert [c.name for c in site.templates[T_LOG].field_cells()] == ["date_line", "vehicle_no", "operator"]


def _write(tmp_path, spec) -> Template:
    p = tmp_path / "template.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return Template(p)


BASE = {"name": "t", "reference_image": "reference.png",
        "regions": [{"name": "main", "grid": {"ys": [0, 10, 20], "xs": [0, 10, 20]}, "header_rows": 1,
                     "columns": [{"idx": 0, "name": "a", "kind": "printed"}], "rows": [{"row": 0, "key": "k"}]}]}


def test_template_validation(tmp_path):
    assert _write(tmp_path, BASE).has_cells
    bad = json.loads(json.dumps(BASE))
    bad["regions"][0]["columns"][0]["kind"] = "typed"
    with pytest.raises(TemplateError):
        _write(tmp_path, bad)
    bad = json.loads(json.dumps(BASE))
    bad["regions"][0]["grid"]["ys"] = [0, 20, 10]
    with pytest.raises(TemplateError):
        _write(tmp_path, bad)
    bad = json.loads(json.dumps(BASE))
    bad["regions"][0]["rows"] = [{"row": 1, "key": "outside"}]
    with pytest.raises(TemplateError):
        _write(tmp_path, bad)
    bad = json.loads(json.dumps(BASE))
    bad["regions"][0]["grid"]["ys"] = [0, 10, 20, 30]
    bad["regions"][0]["rows"] = [{"row": 0, "key": "same"}, {"row": 1, "key": "same"}]
    with pytest.raises(TemplateError):
        _write(tmp_path, bad)
    blank = json.loads(json.dumps(BASE))
    blank["regions"][0]["grid"]["ys"] = [0, 10, 20, 30]
    blank["regions"][0]["rows"] = [{"row": 0, "key": ""}, {"row": 1}]           # 여백 행 두 개 — 키가 겹치지 않아야 한다
    assert [c.row_key for c in _write(tmp_path, blank).cells()] == ["#0", "#1"]
    stub = _write(tmp_path, {"name": "stub", "reference_image": "reference.png"})
    assert not stub.has_cells and stub.handler == "generic"


def test_page_meta_priority(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "site.toml").write_text(
        "[ingest]\ndate_from_filename = '(?P<yy>\\d{2})\\.(?P<mm>\\d{2})\\.(?P<dd>\\d{2})'\n", encoding="utf-8")
    (tmp_path / "labels").mkdir()
    (tmp_path / "labels" / "pages.json").write_text(json.dumps({
        "sheet-1": {"date": "2030-02-01"},
        "30.01.07#3": {"vehicle_no": "V-1", "operator": "ALPHA"},
    }), encoding="utf-8")
    site = SitePack(tmp_path)
    assert site.page_meta("30.01.07", 1) == {"date": "2030-01-07"}                       # 파일명 규칙
    assert site.page_meta("30.01.07", 3) == {"date": "2030-01-07", "vehicle_no": "V-1", "operator": "ALPHA"}
    assert site.page_meta("sheet-1", 1) == {"date": "2030-02-01"}                         # 문서 라벨
    assert site.page_meta("unknown", 1) == {}


def test_missing_site_pack(tmp_path):
    with pytest.raises(FileNotFoundError):
        SitePack(tmp_path / "nope")
