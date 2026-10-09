"""양식의 판: 같은 양식의 개정판을 날짜로 가린다 (tasks/0002 4.4)."""
import json

import pytest
import yaml

from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.forms.classify import FormClassifier
from minedocscan.forms.sitepack import SitePack
from minedocscan.forms.template import Template, TemplateError
from minedocscan.imaging.io import load_pages
from minedocscan.pipeline import Pipeline
from minedocscan.report import build_report
from minedocscan.tools.synth import T_MATRIX, T_MATRIX_V2, generate


@pytest.fixture(scope="module")
def revised(tmp_path_factory):
    """이틀치: 둘째 날부터 행렬 양식이 개정판(T02 자리의 인쇄된 차량·운전자가 바뀜)."""
    root = tmp_path_factory.mktemp("versions")
    synth = generate(root / "data", days=2, seed=0, matrix_revision=True)
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=root / "work", reviews=root / "r.jsonl")
    pipe = Pipeline(settings)
    pipe.run([synth.scans])
    return {"synth": synth, "settings": settings, "pipe": pipe, "root": root}


@pytest.mark.slow                       # 기본 시험 시간을 0008 의 1.15배 안에 (tasks/0009 — CI 의 slow 작업)
def test_pages_go_to_the_version_valid_that_day(revised):
    synth, con = revised["synth"], revised["pipe"].con
    site = SitePack(synth.site)
    v1, v2 = site.templates[T_MATRIX], site.templates[T_MATRIX_V2]
    day1, day2 = (d["date"] for d in synth.truth["days"])
    assert v1.family == v2.family and v1.valid_to == day1 and v2.valid_from == day2
    assert [t.name for t in site.templates_for(day1) if t.family] == [T_MATRIX]
    assert [t.name for t in site.templates_for(day2) if t.family] == [T_MATRIX_V2]
    assert len(site.templates_for(None)) == len(site.templates)
    rep = build_report(con)
    exp = synth.truth["expected"]
    assert rep["pages_by_form"] == exp["pages_by_form"] and rep["pages_by_form"][T_MATRIX_V2] == 1
    assert rep["pages_by_status"] == {"loaded": exp["pages"]}
    # 머리글이 맞으니 배차 관측의 "머리글과 다름"이 판마다 정답과 같다
    assert rep["assignments"] == exp["assignments"]
    assert rep["xcheck_haul"] == exp["xcheck_haul_has_only"]
    got = {(r["work_date"], r["slot"]): (r["header_vehicle_no"], r["header_operator"], r["header_mismatch"])
           for r in con.execute("SELECT * FROM eq_assignment_obs")}
    for d in synth.truth["days"]:
        for t in d["trucks"]:
            hv, ho = d["headers"][t["slot"]]
            assert got[(d["date"], t["slot"])] == (hv, ho, int([t["vehicle_no"], t["operator"]] != [hv, ho]))
    assert got[(day2, "T02")][:2] == ("V-202", "GOLF")


def test_without_dates_the_two_versions_are_indistinguishable(revised):
    """왜 날짜가 필요한가: 두 판은 머리글 몇 글자만 달라 분류 여유가 임계값 아래로 떨어진다."""
    synth, settings = revised["synth"], revised["settings"]
    site = SitePack(synth.site)
    clf = FormClassifier([site.templates[T_MATRIX], site.templates[T_MATRIX_V2]])
    pdf = sorted(synth.scans.glob("*.pdf"))[1]
    gray = list(load_pages(pdf, settings.dpi))[-1][1]                   # 둘째 날의 행렬 쪽(마지막 장)
    r = clf.classify(gray)
    assert r.margin < settings.classify_min_margin
    # 후보를 제한하면 그 판으로 분류된다
    assert clf.classify(gray, candidates=[T_MATRIX_V2]).template == T_MATRIX_V2
    assert clf.classify(gray, candidates=[T_MATRIX]).template == T_MATRIX


def test_overlapping_validity_is_an_error(revised, tmp_path):
    synth = revised["synth"]
    import shutil

    site_dir = tmp_path / "site"
    shutil.copytree(synth.site, site_dir)
    p = site_dir / "templates" / T_MATRIX_V2 / "template.yaml"
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    spec["valid_from"] = synth.truth["days"][0]["date"]                 # 옛 판의 마지막 날과 겹친다
    p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(TemplateError, match="겹칩니다"):
        SitePack(site_dir)
    spec["valid_from"] = "2030-13-01"
    p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(TemplateError, match="YYYY-MM-DD"):
        Template(p)


def test_info_shows_family_and_validity(revised, capsys):
    synth = revised["synth"]
    assert main(["info", "--site", str(synth.site), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    by = {t["name"]: t for t in out["site"]["templates"]}
    assert by[T_MATRIX]["family"] == by[T_MATRIX_V2]["family"] and by[T_MATRIX_V2]["valid_from"]
    assert by["synth_inspection"]["family"] is None
    assert main(["info", "--site", str(synth.site)]) == 0
    assert "계열" in capsys.readouterr().out
