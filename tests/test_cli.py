"""명령줄을 처음부터 끝까지: synth → run → report → eval → regress → template init."""
import json

import pytest

from minedocscan.cli import main


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("cli")
    assert main(["synth", str(root / "data"), "--days", "1", "--seed", "2"]) == 0
    common = ["--site", str(root / "data" / "site"), "--archive-root", str(root / "data" / "scans"),
              "--work-root", str(root / "work")]
    return root, common


def _json(capsys, argv):
    capsys.readouterr()
    code = main(argv + ["--json"])
    return code, json.loads(capsys.readouterr().out)


def test_info(env, capsys):
    _root, common = env
    code, out = _json(capsys, ["info"] + common)
    assert code == 0
    assert {t["name"] for t in out["site"]["templates"]} == {"synth_inspection", "synth_haul_log", "synth_haul_matrix"}
    assert "null" in out["backends"]["recognizers"] and "haul" in out["backends"]["handlers"]


def test_run_report_eval(env, capsys):
    root, common = env
    expected = json.loads((root / "data" / "truth.json").read_text(encoding="utf-8"))["expected"]
    answers = str(root / "data" / "answers.json")

    code, out = _json(capsys, ["run", "--fresh"] + common)                 # 경로를 안 주면 archive_root 전체
    assert code == 0 and out["report"]["pages_by_form"] == expected["pages_by_form"]
    assert out["report"]["xcheck_haul"] == expected["xcheck_haul_has_only"]

    code, out = _json(capsys, ["eval", "--answers", answers] + common)
    assert code == 0 and out["fields"]["field_accuracy"] < 1.0               # 인식기가 없으니 틀려야 정상

    code, out = _json(capsys, ["run", "--answers", answers] + common)      # 같은 DB 에 다시 — 덮어쓴다
    assert code == 0 and out["report"]["pages"] == expected["pages"]
    assert out["report"]["xcheck_haul"] == expected["xcheck_haul_with_trips"]

    code, out = _json(capsys, ["eval", "--answers", answers] + common)
    assert code == 0 and out["fields"]["cer"] == 0.0 and out["fields"]["field_accuracy"] == 1.0

    code, out = _json(capsys, ["report"] + common)
    assert code == 0 and out["report"]["documents"] == 1 and len(out["xcheck_by_date"]) == 1
    assert main(["report"] + common) == 0
    assert "교차검증" in capsys.readouterr().out


def test_regress_roundtrip(env, capsys):
    root, common = env
    assert main(["regress", "--update", "--inputs", "."] + common) == 0
    baseline = root / "data" / "site" / "expected" / "regression.json"
    assert json.loads(baseline.read_text(encoding="utf-8"))["inputs"] == ["."]
    assert main(["regress"] + common) == 0                                    # 같은 코드, 같은 데이터 → 같아야 한다
    spec = json.loads(baseline.read_text(encoding="utf-8"))
    spec["report"]["xcheck_haul"]["mismatch"] += 1
    baseline.write_text(json.dumps(spec), encoding="utf-8")
    capsys.readouterr()
    assert main(["regress"] + common) == 1
    assert "xcheck_haul.mismatch" in capsys.readouterr().out


def test_template_init(env, tmp_path, capsys):
    root, _common = env
    site = tmp_path / "newsite"
    (site / "templates").mkdir(parents=True)
    ref = root / "data" / "site" / "templates" / "synth_haul_matrix" / "reference.png"
    assert main(["template", "init", str(ref), "--name", "new_form", "--roi", "130,310,1420,1430",
                 "--header-rows", "2", "--site", str(site)]) == 0
    from minedocscan.forms.template import Template

    tpl = Template(site / "templates" / "new_form" / "template.yaml")
    reg = tpl.regions[0]
    assert len(reg["grid"]["xs"]) == 7 and len(reg["grid"]["ys"]) == 15
    assert len(reg["rows"]) == 12 and len(tpl.cells()) == 12 * 6
    assert (site / "templates" / "new_form" / "reference.png").exists()
    with pytest.raises(FileExistsError):
        main(["template", "init", str(ref), "--name", "new_form", "--site", str(site)])


def test_run_without_site_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("MINEDOCSCAN_SITE", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["run", str(tmp_path)])
