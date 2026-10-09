"""자가 시험 `minedocscan selftest` (tasks/0009 4.6): 통과하고 결과 파일에 사용자의 경로·이름이 없다, 검사를 하나씩 깨뜨리면 종료 코드 1 과
그 검사의 이름, 받지 않는 스키마 이름, (PostgreSQL 이 있으면) 실은 스키마가 끝나면 없다.

합성 묶음은 한 번 만들어 복사한다 (selftest.make_synth 를 바꿔 끼운다). 렌더링·분류·정합은 conftest.fast_imaging 으로 저장해 둔 결과를 쓴다.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import fast_imaging
from minedocscan import selftest
from minedocscan.cli import main

PG = os.environ.get("MINEDOCSCAN_TEST_PG_URL")


@pytest.fixture(scope="session")
def selftest_synth(tmp_path_factory):
    root = tmp_path_factory.mktemp("selftest_synth")
    return selftest.make_synth(root / "합성")


@pytest.fixture
def quick(selftest_synth, monkeypatch):
    """합성 묶음은 복사, 렌더링·분류·정합은 저장해 둔 결과로."""
    fast_imaging(monkeypatch)
    src = Path(selftest_synth.root)

    def copied(out: Path):
        shutil.copytree(src, out)
        rel = lambda p: out / Path(p).relative_to(src)                 # noqa: E731
        return replace(selftest_synth, root=out, site=rel(selftest_synth.site), scans=rel(selftest_synth.scans),
                       truth_path=rel(selftest_synth.truth_path), answers_path=rel(selftest_synth.answers_path))

    monkeypatch.setattr(selftest, "make_synth", copied)
    for k in [k for k in os.environ if k.startswith("MINEDOCSCAN_")]:
        monkeypatch.delenv(k)


def run_cli(tmp_path, capsys, *extra) -> tuple[int, dict, str]:
    out = tmp_path / "결과"
    code = main(["selftest", "--out", str(out), *extra])
    text = capsys.readouterr().out
    return code, json.loads((out / "selftest.json").read_text(encoding="utf-8")), text


def test_selftest_passes_and_the_results_name_no_user_path(quick, tmp_path, capsys, monkeypatch):
    made = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(selftest.tempfile, "mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1])
    code, r, text = run_cli(tmp_path, capsys)
    assert code == 0, text
    assert r["passed"] and r["failed"] == []
    status = {c["name"]: c["status"] for c in r["checks"]}
    assert status == {**{c.name: "passed" for c in selftest.CHECKS}, "publish": "skipped"}
    assert next(c for c in r["checks"] if c["name"] == "publish")["reason"].startswith("요청하지 않았다")
    env = r["environment"]
    assert env["python"] and env["cpu_count"] and "numpy" in env["dependencies"] and "pypdfium2" in env["dependencies"]
    assert not Path(made[0]).exists()                                   # 임시 폴더는 지웠다
    files = [(tmp_path / "결과" / n).read_text(encoding="utf-8") for n in ("selftest.json", "selftest.md")]
    user = getpass.getuser()
    for body in files:
        for p in (made[0], str(tmp_path), str(Path.home()), tempfile.gettempdir(), Path(made[0]).name):
            assert p not in body
        if len(user) >= 3:
            assert user not in body
    assert "통과" in files[1] and "| `excel` | 통과 |" in files[1]


def _break_synth(monkeypatch):
    monkeypatch.setattr(selftest, "make_synth", lambda out: (_ for _ in ()).throw(RuntimeError("부러뜨렸다")))


def _break_intake(monkeypatch):
    import minedocscan.pipeline.runner as runner

    monkeypatch.setattr(runner, "align_to_template", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("정합 실패")))


def _break_recognition(monkeypatch):
    from minedocscan.recognize import OracleRecognizer

    real = OracleRecognizer.recognize
    monkeypatch.setattr(OracleRecognizer, "recognize", lambda self, crops, ctxs: [
        replace(r, text=r.text + "9") if r.text else r for r in real(self, crops, ctxs)])


def _break_reprocess(monkeypatch):
    import minedocscan.store.db as db

    monkeypatch.setattr(db, "PAGE_TABLES", tuple(t for t in db.PAGE_TABLES if t != "eq_usage_daily"))   # 버린 쪽의 행이 남는다


def _break_excel(monkeypatch):
    import minedocscan.export.xlsx as xlsx

    real = xlsx.text
    monkeypatch.setattr(xlsx, "text", lambda v: v + 1 if isinstance(v, int) and not isinstance(v, bool) else real(v))


def _break_masked(monkeypatch):
    import minedocscan.export.masked as masked

    monkeypatch.setattr(masked, "mask", lambda gray, boxes: gray.copy())


def _break_serve(monkeypatch):
    monkeypatch.setattr(selftest, "serve_command", lambda config, logs: [sys.executable, "-c", "pass"])


BREAKS = {"synth": _break_synth, "intake": _break_intake, "recognition": _break_recognition, "reprocess": _break_reprocess,
          "excel": _break_excel, "masked": _break_masked, "serve": _break_serve}


def test_every_check_is_broken_by_one_test():
    assert set(BREAKS) | {"publish"} == {c.name for c in selftest.CHECKS}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(BREAKS))
def test_a_broken_check_exits_1_with_its_name(name, quick, tmp_path, capsys, monkeypatch):
    BREAKS[name](monkeypatch)
    code, r, text = run_cli(tmp_path, capsys)
    assert code == 1
    assert r["failed"][0] == name and not r["passed"]
    line = next(x for x in text.splitlines() if x.startswith("실패") and f" {name} " in x)
    assert "—" in line                                                 # 왜 — 한 줄
    later = [c for c in selftest.CHECKS if name in c.needs]
    for c in later:                                                    # 그것에 기대는 검사는 건너뛴다
        assert next(x for x in r["checks"] if x["name"] == c.name)["status"] == "skipped"


@pytest.mark.slow
def test_recognition_counts_the_cells_not_only_the_ones_it_has(quick, tmp_path, monkeypatch):
    """정답이 모르는 칸(값 없는 칸·정답 없는 표)이 빠져도 — 쪽마다 손글씨 칸 수를 템플릿과 센다."""
    import minedocscan.evaluate.fields as fields

    monkeypatch.setattr(fields, "evaluate_fields", lambda *a, **kw: {"answers_not_in_db": 0})

    def dropped(ctx):
        con = ctx.pipe.con
        con.execute("DELETE FROM doc_field WHERE field_id IN (SELECT f.field_id FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                    "WHERE f.kind LIKE 'handwritten%' AND f.has_value_raw = 0 AND p.status = 'loaded' LIMIT 3)")
        con.commit()
        return selftest.check_recognition(ctx)

    checks = [c if c.name != "recognition" else replace(c, fn=dropped) for c in selftest.CHECKS
              if c.name in ("synth", "intake", "recognition")]
    r = selftest.run(tmp_path / "결과", checks=checks, echo=lambda s: None)
    rec = next(c for c in r["checks"] if c["name"] == "recognition")
    assert rec["status"] == "failed" and "칸 수가 템플릿과 다르다" in rec["reason"]


def test_reasons_are_one_line_and_scrubbed_before_they_are_cut(tmp_path):
    scrub = selftest._scrubber(tmp_path)
    text = "x" * (selftest.REASON_CHARS - 5) + "\n" + str(tmp_path / "작업" / "a.xlsx")
    out = selftest._line(scrub(text))
    assert "\n" not in out and str(tmp_path)[:6] not in out[selftest.REASON_CHARS - 10:]
    assert out.endswith("…") and len(out) == selftest.REASON_CHARS + 1


def test_publish_schema_must_carry_the_prefix(tmp_path, capsys):
    for bad in ("minedocscan", "public", "Minedocscan_selftest_a", "minedocscan_selftest_a-b", "x" + selftest.SCHEMA_PREFIX):
        assert main(["selftest", "--out", str(tmp_path), "--publish-schema", bad]) == 2
        assert "minedocscan_selftest_" in capsys.readouterr().err
    assert not (tmp_path / "selftest.json").exists()
    assert selftest.valid_schema("minedocscan_selftest_ci_1")


@pytest.mark.postgres
@pytest.mark.skipif(not PG, reason="MINEDOCSCAN_TEST_PG_URL 이 없다")
def test_publish_check_leaves_no_schema_and_refuses_one_it_did_not_make(quick, tmp_path, capsys, monkeypatch):
    import psycopg

    schema = f"{selftest.SCHEMA_PREFIX}{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", PG)

    def exists() -> bool:
        with psycopg.connect(PG, autocommit=True) as c:
            return c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)).fetchone() is not None

    code, r, text = run_cli(tmp_path, capsys, "--publish-schema", schema)
    assert code == 0, text
    pub = next(c for c in r["checks"] if c["name"] == "publish")
    assert pub["status"] == "passed" and pub["detail"]["rows"] > 0 and not exists()
    assert PG not in json.dumps(r) and PG not in text
    with psycopg.connect(PG, autocommit=True) as c:                    # 남이 만든 스키마 — 쓰지도 지우지도 않는다
        c.execute(f'CREATE SCHEMA "{schema}"')
        c.execute(f'CREATE TABLE "{schema}".keep_me (x int)')
    try:
        code, r, _text = run_cli(tmp_path, capsys, "--publish-schema", schema)
        assert code == 1 and r["failed"] == ["publish"]
        with psycopg.connect(PG, autocommit=True) as c:
            assert c.execute(f'SELECT COUNT(*) FROM "{schema}".keep_me').fetchone()[0] == 0
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.postgres
@pytest.mark.skipif(not PG, reason="MINEDOCSCAN_TEST_PG_URL 이 없다")
def test_a_broken_publish_exits_1_with_its_name(quick, tmp_path, capsys, monkeypatch):
    from minedocscan.publish import core

    schema = f"{selftest.SCHEMA_PREFIX}{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("MINEDOCSCAN_PUBLISH_URL", PG)
    monkeypatch.setattr(core.Target, "insert", lambda self, table, cols, rows: None)   # 아무것도 넣지 않는다 → --check 가 다르다
    try:
        code, r, _text = run_cli(tmp_path, capsys, "--publish-schema", schema)
        assert code == 1 and r["failed"] == ["publish"]
    finally:
        import psycopg

        with psycopg.connect(PG, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
