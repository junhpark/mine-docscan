"""시험 성적서의 생성기 (tasks/0009 4.9): 가짜 CI 산출물로 1–7절이 채워지고 8절(실데이터)은 칸과 명령만, 성적서에 경로·이름이 없다,
산출물이 없는 작업은 "산출물 없음" 으로 (실패한 작업도 성적서에)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("test_report_script", ROOT / "scripts" / "test_report.py")
tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tr)

ENV = {"minedocscan": "1.0.0", "os": "Linux 6.1", "python": "3.12.4", "dependencies": {"numpy": "2.1.0", "cv2": "5.0.0"},
       "cpu_count": 4, "memory_gb": 15.6}


def junit_xml(tests: int, failures: int, skipped: int, skip_msg: str) -> str:
    cases = "".join(f'<testcase classname="tests.test_a" name="t{i}" time="0.1"/>' for i in range(tests - failures - skipped))
    cases += "".join(f'<testcase classname="tests.test_b" name="f{i}"><failure message="/home/runner/secret/x.py boom"/></testcase>'
                     for i in range(failures))
    cases += "".join(f'<testcase classname="tests.test_c" name="s{i}"><skipped message="{skip_msg}"/></testcase>' for i in range(skipped))
    return (f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{tests}" failures="{failures}" errors="0" '
            f'skipped="{skipped}" time="12.5">{cases}</testsuite></testsuites>')


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_the_report_fills_sections_one_to_seven_and_leaves_real_data_to_people(tmp_path):
    art = tmp_path / "artifacts"
    for job in ("test-3.11", "test-3.12", "lowest", "slow", "postgres"):
        write(art / f"junit-{job}" / "junit.xml", junit_xml(10, 0, 2, "Skipped: MINEDOCSCAN_TEST_PG_URL 이 없다 (/home/runner/work/x)"))
        write(art / f"junit-{job}" / "env.json", json.dumps(ENV))
    # windows 작업은 실패해서 산출물이 없다
    selftest = {"passed": True, "seconds": 49.4, "environment": ENV,
                "checks": [{"name": "synth", "title": "합성", "status": "passed", "seconds": 2.9, "reason": ""},
                           {"name": "publish", "title": "통합 DB", "status": "skipped", "seconds": 0, "reason": "요청하지 않았다"}]}
    write(art / "junit-test-3.12" / "selftest" / "selftest.json", json.dumps(selftest, ensure_ascii=False))
    write(art / "junit-test-3.12" / "licenses.txt", "ok  numpy 2.1 — BSD-3-Clause\nok  openpyxl 3.1.5 — MIT\nok  PyYAML 6 — MIT\n")
    rep = art / "windows-install-report"
    write(rep / "selftest" / "selftest.json", json.dumps(selftest, ensure_ascii=False))
    write(rep / "dist" / "bundle-report.json", json.dumps({"zip_bytes": 79728580, "wheels": 12, "python": "3.12.10",
                                                           "dll": {"needed_from_system": {}, "msvcp140_needed_from_system": False}}))
    write(rep / "dist" / "licenses.txt", "ok  pip 26.2.1 — MIT\nNO  odd 1.0 — ?\nok  python (embeddable) 3.12.10 — PSF-2.0\n"
                                         "ok  pip  — MIT\nNO  missing (설치되지 않음) — ?\n")              # licenses.py 가 찍는 꼴 그대로
    write(art / "junit-test-3.11" / "job-result.txt", "failure\n")    # 행렬의 한 갈래만 실패
    write(rep / "install-steps.json", json.dumps({"bundle": {"outcome": "success", "outputs": {"bundle_mb": "76.0"}},
                                                  "install": {"outcome": "success", "outputs": {"seconds": "15"}},
                                                  "uninstall": {"outcome": "failure", "outputs": {}}}))
    write(art / "v2-metrics" / "v2-metrics.json", json.dumps({"days": 3, "seed": 0, "normal": {"fuel_log": {
        "pages": 3, "classified": 3, "loaded": 3, "grid_err_median": 0.0, "cells": 81, "sent_cells": 50, "oracle_cer": 0.0,
        "presence_precision": 1.0, "presence_recall": 0.9846}}, "rough": {}}))
    needs = {"test": {"result": "success"}, "windows": {"result": "failure"}, "windows-install": {"result": "failure"}}
    text = tr.build(art, needs, "1.0.0", ROOT / "docs" / "test-report")
    for title in ("## 1. 시험 환경", "## 2. 자동 시험", "## 3. 자가 시험", "## 4. 설치 시험", "## 5. 규모", "## 6. 확장성", "## 7. 라이선스",
                  "## 8. 실데이터"):
        assert title in text
    assert "| 우분투 · 파이썬 3.12 | 통과 | 10 | 8 | 0 | 2 | 12.5 |" in text
    assert "| 우분투 · 파이썬 3.11 | 실패 |" in text                     # 갈래마다의 결과 (needs 의 test 는 성공)
    assert "작업 결과: 실패" in text
    assert "| 윈도우 · 파이썬 3.12 | 실패 |" in text and "산출물 없음" in text      # 실패한 작업도 성적서에
    assert "| 설치 (망을 막고) | 통과 | seconds 15 |" in text and "| 지우기 — 프로그램만 | 실패 |" in text
    assert "msvcp140.dll 필요 없음" in text and "| fuel_log | 보통 | 3 | 3 | 3 | 0.0 | 81 | 50 | 0.0 | 1.0 | 0.9846 |" in text
    assert "배포판 5개, 허용 목록 밖 2개 (odd 1.0, missing (설치되지 않음))" in text and "| MIT | 2 |" in text and "| PSF-2.0 | 1 |" in text
    assert "`minedocscan regress`" in text                              # 8절: 칸과 명령
    assert "/home/runner" not in text and "boom" not in text            # 경로·실패의 글 없이
    assert "<경로>" in text                                             # 건너뛴 이유의 경로는 지웠다


def test_the_report_runs_without_artifacts(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NEEDS", json.dumps({"test": {"result": "cancelled"}}))
    assert tr.main(["build", "--artifacts", str(tmp_path / "없음"), "--out", str(tmp_path / "out"), "--version", "1.0.0"]) == 0
    text = (tmp_path / "out" / "시험성적서-1.0.0.md").read_text(encoding="utf-8")
    assert "| 우분투 · 파이썬 3.11 | 취소 |" in text and "## 8. 실데이터" in text


def test_env_names_no_host_or_path(tmp_path):
    out = tmp_path / "env.json"
    assert tr.main(["env", "--out", str(out)]) == 0
    env = json.loads(out.read_text(encoding="utf-8"))
    assert env["python"] and "numpy" in env["dependencies"]
    assert str(Path.home()) not in out.read_text(encoding="utf-8")
