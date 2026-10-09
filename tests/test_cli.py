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


@pytest.mark.slow                       # 기본 시험 시간을 0008 의 1.15배 안에 (tasks/0009 — CI 의 slow 작업)
def test_regress_roundtrip(env, capsys):
    root, common = env
    assert main(["run", "--skip-existing"] + common) == 0                    # 작업 DB 가 있게 (slow 만 돌 때 — 앞 시험 없이)
    assert main(["regress", "--update", "--inputs", "."] + common) == 0
    baseline = root / "data" / "site" / "expected" / "regression.json"
    assert json.loads(baseline.read_text(encoding="utf-8"))["inputs"] == ["."]
    capsys.readouterr()
    assert main(["regress"] + common) == 0                                    # 같은 코드, 같은 데이터 → 같아야 한다
    out = capsys.readouterr().out
    assert "기준과 같습니다" in out and "기준이 아직 없습니다" not in out and "새 항목" not in out
    # 검수가 쌓여도 회귀는 기계 값만 본다 — 사이트 팩의 검수 파일을 읽어 들이지 않는다
    from minedocscan.review.store import Review, append
    from minedocscan.store.db import open_db

    con = open_db(f"sqlite:///{(root / 'work' / 'minedocscan.db').as_posix()}")
    fid = con.execute("SELECT field_id FROM doc_field WHERE kind='handwritten_number' ORDER BY field_id").fetchone()[0]   # 어느 칸이든
    con.close()
    append(root / "data" / "site" / "reviews" / "reviews.jsonl", Review(fid, "value", "9", "jp"))
    assert main(["regress"] + common) == 0
    spec = json.loads(baseline.read_text(encoding="utf-8"))
    spec["report"]["xcheck_haul"]["mismatch"] += 1
    baseline.write_text(json.dumps(spec), encoding="utf-8")
    capsys.readouterr()
    assert main(["regress"] + common) == 1
    assert "xcheck_haul.mismatch" in capsys.readouterr().out


def test_regress_output_lines(env, monkeypatch, capsys):
    """regress 의 안내문: 같음 / 어긋남 / 기준 없음, 그리고 기준에 없던 새 묶음은 따로 (실행 없이 결과만 바꿔 본다)."""
    import minedocscan.evaluate.regression as rg

    _root, common = env
    base = {"diffs": [], "new_keys": [], "report": {}, "baseline": "b.json", "updated": False, "had_baseline": True, "ok": True}
    cases = [({}, 0, ["기준과 같습니다"], ["기준이 아직 없습니다", "새 항목"]),
             ({"new_keys": ["warnings.n"]}, 0, ["기준과 같습니다", "기준에 없던 새 항목 1개", "warnings.n"], ["기준이 아직 없습니다"]),
             ({"diffs": [("fields.pending", 5, 6)], "ok": False}, 1, ["기준과 다른 항목 1개", "fields.pending"], ["기준과 같습니다"]),
             ({"had_baseline": False}, 0, ["기준이 아직 없습니다"], ["기준과 같습니다"])]
    for change, code, want, unwanted in cases:
        monkeypatch.setattr(rg, "run_regression", lambda *a, _r=base | change, **k: _r)
        capsys.readouterr()
        assert main(["regress"] + common) == code
        out = capsys.readouterr().out
        assert all(w in out for w in want) and not any(u in out for u in unwanted), (change, out)


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


# ── tasks/0009 4.1 사 ──────────────────────────────────────────────────────────
_ENV = ("MINEDOCSCAN_CONFIG", "MINEDOCSCAN_SITE", "MINEDOCSCAN_WORK_ROOT", "MINEDOCSCAN_DB_URL", "MINEDOCSCAN_ARCHIVE_ROOT",
        "MINEDOCSCAN_PUBLISH_URL", "MINEDOCSCAN_INBOX", "MINEDOCSCAN_EXCEL_DIR", "MINEDOCSCAN_REVIEWS", "MINEDOCSCAN_DAMAGED_PDF")


def test_a_postgres_work_db_url_shows_only_host_and_db(tmp_path, monkeypatch, capsys):
    """MINEDOCSCAN_DB_URL(작업 DB)에 비밀번호가 든 PostgreSQL URL 을 잘못 넣어도 info 와 store.db 의 오류 글에는 호스트·DB 만.
    sqlite:///… 는 지금처럼 경로를 보인다."""
    from pathlib import Path

    from minedocscan.cli import _db_label
    from minedocscan.config import Settings
    from minedocscan.store.db import open_db, open_db_readonly

    secret = "s3cr3t-p4ss"
    url = f"postgresql://writer:{secret}@db.example.invalid:5432/site"
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINEDOCSCAN_DB_URL", url)
    code, out = _json(capsys, ["info"])
    assert code == 0 and out["settings"]["db_url"] == "db.example.invalid:5432/site"
    assert secret not in json.dumps(out, ensure_ascii=False) and "writer" not in json.dumps(out, ensure_ascii=False)
    assert main(["info"]) == 0
    printed = "".join(capsys.readouterr())
    assert "db.example.invalid:5432/site" in printed and secret not in printed and "writer" not in printed
    for fn in (open_db, open_db_readonly):
        with pytest.raises(NotImplementedError) as ei:
            fn(url)
        assert "db.example.invalid:5432/site" in str(ei.value), fn
        assert secret not in str(ei.value) and "writer" not in str(ei.value), fn
    assert _db_label(Settings(db_url=url)) == "db.example.invalid:5432/site"
    assert _db_label(Settings(work_root=Path("w"))) == "sqlite:///w/minedocscan.db"
    assert _db_label(Settings(db_url="sqlite:///C:/작업 폴더/minedocscan.db")) == "sqlite:///C:/작업 폴더/minedocscan.db"


# 하위 명령의 하위 명령 앞에 준 공통 옵션이 버려졌다 (하위 파서가 기본값으로 덮었다) — 다섯 명령 모두, 앞에 줘도 뒤에 줘도 같게
COMMON_OPTS = ["--config", "c.toml", "--site", "S", "--archive-root", "A", "--work-root", "W", "--db-url", "sqlite:///D.db",
               "--json"]
COMMON_DEST = {"config": "c.toml", "site": "S", "archive_root": "A", "work_root": "W", "db_url": "sqlite:///D.db", "json": True}
NESTED = [(["export"], ["excel", "OUT"]), (["export"], ["masked-pages", "OUT", "--date", "2030-01-07"]),
          (["review"], ["serve", "--reviewer", "jp"]), (["review"], ["stats"]), (["review"], ["export-crops", "OUT"]),
          (["review"], ["export-answers", "a.json"]),
          (["doc"], ["list"]), (["doc"], ["date", "d1", "2030-01-07"]), (["doc"], ["keep", "d1-p1"]),
          (["template"], ["check", "T"]), (["template"], ["preview", "T"]), (["template"], ["init", "IMG", "--name", "new_form"]),
          (["recognizer"], ["list"]), (["recognizer"], ["eval", "--crops", "C", "--model", "M"]),
          (["recognizer"], ["train", "--name", "digits-v1"])]


@pytest.mark.parametrize("cmd, rest", NESTED, ids=[" ".join(c + r[:1]) for c, r in NESTED])
def test_common_options_before_a_nested_subcommand_are_kept(cmd, rest):
    from minedocscan.cli import build_parser

    ap = build_parser()
    before = vars(ap.parse_args([*cmd, *COMMON_OPTS, *rest]))
    after = vars(ap.parse_args([*cmd, *rest, *COMMON_OPTS]))
    assert before == after
    assert {k: before[k] for k in COMMON_DEST} == COMMON_DEST
    none = vars(ap.parse_args([*cmd, *rest]))                               # 어디에도 주지 않으면 지금처럼 None·False
    assert {k: none[k] for k in COMMON_DEST} == {**dict.fromkeys(COMMON_DEST), "json": False}
    both = ap.parse_args([*cmd, "--work-root", "W1", *rest, "--work-root", "W2"])   # 둘 다 주면 뒤의 것
    assert both.work_root == "W2"


def test_every_nested_subcommand_leaves_the_common_options_to_its_parent():
    """하위 명령이 있는 다섯 명령(export·review·doc·template·recognizer)의 모든 하위 명령: 공통 옵션의 기본값이 SUPPRESS."""
    import argparse

    from minedocscan.cli import build_parser

    def subs(p):
        return next((a.choices for a in p._actions if isinstance(a, argparse._SubParsersAction)), {})

    nested = {name: p for name, p in subs(build_parser()).items() if subs(p)}
    assert set(nested) == {"export", "review", "doc", "template", "recognizer"}
    for name, p in nested.items():
        for sub, sp in subs(p).items():
            for opt in ("--config", "--site", "--archive-root", "--work-root", "--db-url", "--json"):
                assert sp._option_string_actions[opt].default is argparse.SUPPRESS, (name, sub, opt)


def test_doc_list_uses_the_options_given_before_list(tmp_path, monkeypatch, capsys):
    """doc --site S --work-root W --json list: 그 작업 폴더의 DB 를 JSON 으로 (전에는 ./work 에 새 DB 를 만들고 글로 찍었다)."""
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    site = tmp_path / "site"
    site.mkdir()
    capsys.readouterr()
    assert main(["doc", "--site", str(site), "--work-root", str(tmp_path / "w"), "--json", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == {"documents": []}
    assert (tmp_path / "w" / "minedocscan.db").is_file() and not (tmp_path / "work").exists()


def test_info_shows_the_site_name_and_the_split_salt_to_keep(env, tmp_path, capsys, monkeypatch):
    """info: 적힌 [site] name 과, 평가셋의 소금값이 이름을 따르고 있으면 그 값 — 이름이 없으면 폴더 이름이라 이름을 처음 적을 때도
    바뀐다. 옮겨 적을 값을 보여 준다 (tasks/0009 4.1 다, 8절). 소금값이 적혀 있으면 아무 말도 하지 않는다."""
    import re
    import shutil

    root, _common = env
    for k in ("MINEDOCSCAN_SITE", "MINEDOCSCAN_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    site = tmp_path / "pack-a"
    shutil.copytree(root / "data" / "site", site)
    toml = site / "site.toml"
    text = toml.read_text(encoding="utf-8")
    base = ["info", "--site", str(site), "--work-root", str(tmp_path / "w")]
    code, out = _json(capsys, base)                                    # 합성 사이트 팩: 이름과 소금값이 다 적혀 있다
    assert code == 0 and out["site"]["declared_name"] == "synthetic" and not out["site"]["split_salt_follows_name"]
    assert out["site"]["split_salt"] is None
    toml.write_text(re.sub(r"(?m)^split_salt\s*=.*$", "", text), encoding="utf-8")
    code, out = _json(capsys, base)                                    # 소금값이 없다 — 사이트 이름을 따른다
    assert out["site"]["split_salt_follows_name"] and out["site"]["split_salt"] == "synthetic"
    assert main(base) == 0
    printed = capsys.readouterr().out
    assert '사이트 이름 "synthetic"' in printed and '[eval] split_salt = "synthetic"' in printed
    toml.write_text(re.sub(r"(?m)^(split_salt|name)\s*=.*$", "", text, count=0), encoding="utf-8")
    code, out = _json(capsys, base)                                    # 이름도 없다 — 폴더 이름을 따른다
    assert out["site"]["declared_name"] is None and out["site"]["split_salt"] == "pack-a"
    assert main(base) == 0
    printed = capsys.readouterr().out
    assert "[site] name: 없음" in printed and '폴더 이름 "pack-a"' in printed and '[eval] split_salt = "pack-a"' in printed
