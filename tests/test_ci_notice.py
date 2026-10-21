"""CI 의 알림 (tasks/0010 4.6): ASCII 만, key=value, 수만 — 경로·사용자 이름이 들어갈 자리가 없다. GitHub 의 이스케이프."""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ci_notice import fields, notice  # noqa: E402


def test_a_notice_is_one_ascii_line_of_numbers():
    s = notice("windows tests", {"passed": 571, "skipped": 26, "failed": 0, "seconds": 712.4}, [("ok", True), ("x", None)])
    assert s == "::notice title=windows tests::passed=571 skipped=26 failed=0 seconds=712.4%0Aok=1 x=-"
    assert s.isascii() and "\n" not in s
    assert fields({"a": 0.123456, "b": float("nan"), "c": 2.0}) == "a=0.1235 b=nan c=2"
    assert notice("t", "k=1").startswith("::notice title=t::k=1")
    assert notice("t", {"p": 5}, level="warning") == "::warning title=t::p=5"


@pytest.mark.parametrize("value", ["C:\\Users\\jp", "/home/jp/work", "a b", "이름", "x:y", "a,b", "50%"])
def test_values_that_could_carry_a_path_or_a_name_are_refused(value):
    with pytest.raises(ValueError):
        notice("t", {"v": value})
    with pytest.raises(ValueError):
        notice("t", f"v={value}")


def test_titles_and_keys_are_plain():
    for bad in ("a:b", "a,b", "제목", ""):
        with pytest.raises(ValueError):
            notice(bad, {"v": 1})
    for bad in ("Key", "1a", "a-b", "a b"):
        with pytest.raises(ValueError):
            fields({bad: 1})


def test_the_signature_probe_line_has_no_path_or_user(tmp_path, monkeypatch):
    """sig_probe 의 알림 줄 (단계 2): 후보마다 수만 — 경로·사용자 이름 없이 ASCII."""
    import sig_probe

    res = {name: {**{c: {"n": 3, "median": 0.99, "p5": 0.98, "min": 0.97, "max": 1.0} for c in sig_probe.CASES},
                  "rescan": {"n": 4, "min": 0.99}, "other": {"n": 5, "max": 0.59}, "ms": 20.1, "chars": 20062, "ok": True}
           for name in sig_probe.CANDIDATES}
    head = {"platform": sys.platform, "opencv": "5.0.0", "python": "3.12.10", "pages": 17, "rescans": 4, "other_pairs": 15,
            "production": "v2"}
    s = notice("sig_probe linux", *sig_probe.lines(res, head))
    assert s.isascii() and str(tmp_path) not in s and str(Path.home()) not in s
    user = getpass.getuser()
    assert len(user) < 3 or user not in s.split("::", 2)[2]
    assert s.count("%0A") == len(res)


def test_the_signature_probe_counts_incomparable_pages_apart(monkeypatch):
    """서명을 견줄 수 없는(전부 0 인 — 빈) 쪽이 있으면 그 쌍은 분포(최소·5 %)에서 빠지고 따로 센다 — 0.0 으로 세지 않는다 (tasks/0011 4.8).
    그런 쪽의 수(no_signature)도 따로. 견줄 수 없는 다시 스캔 쌍은 통과가 아니다 (현장의 dup_min_sim 도 붙잡지 못한다)."""
    import cv2
    import numpy as np
    import sig_probe

    monkeypatch.setattr(sig_probe, "CANDIDATES", {"v1": lambda b, tpl: sig_probe.sigs.signature(None, None, binary=b)})
    rng = np.random.default_rng(0)

    def page(blank: bool) -> dict:
        g = np.full((320, 480), 245, np.uint8)
        for _ in range(0 if blank else 40):
            x, y = int(rng.integers(20, 440)), int(rng.integers(20, 280))
            cv2.line(g, (x, y), (x + 20, y + 15), 30, 3)
        return {"gray": g, "tpl": None, "day": "d", "family": "f", "original": True}

    res = sig_probe.measure([page(False), page(False), page(True)], rescans=[(0, 1)], others=[(0, 1), (0, 2), (1, 2)])
    r = res["v1"]
    for case in sig_probe.CASES:
        assert r[case]["n"] == 2 and r[case]["none"] == 1 and r[case]["min"] > 0.0 and r[case]["p5"] > 0.0
    assert (r["other"]["n"], r["other"]["none"]) == (1, 2) and r["other"]["min"] > 0.0
    assert r["no_signature"] == 1 and r["rescan"]["none"] == 0       # 빈 쪽 하나 — 다시 스캔 쌍(0, 1)은 견줄 수 있다
    row = dict(sig_probe.lines(res, {"pages": 3})[1])
    assert row["none"] == len(sig_probe.CASES) + 2 and row["no_sig"] == 1
    bad = sig_probe.measure([page(False), page(False), page(True)], rescans=[(0, 2)], others=[(0, 1)])["v1"]
    assert (bad["rescan"]["n"], bad["rescan"]["none"]) == (0, 1) and bad["ok"] is False


def test_the_readers_take_only_numbers_from_their_files(tmp_path, capsys):
    """--junit·--selftest·--licenses·--v2: 파일의 수만 읽는다 — 시험 이름·실패 글·경로·이름이 든 파일이어도 알림에는 수만 (4.6)."""
    import json

    from ci_notice import main

    secret = str(tmp_path / "작업" / "ALPHA")
    (tmp_path / "junit.xml").write_text(
        f'<testsuites><testsuite tests="10" skipped="2" failures="1" errors="0" time="12.6">'
        f'<testcase name="t {secret}"><failure message="{secret}">{secret}</failure></testcase></testsuite></testsuites>',
        encoding="utf-8")
    (tmp_path / "selftest.json").write_text(json.dumps({"passed": True, "seconds": 40.9, "checks": [
        {"name": "synth", "status": "passed", "message": secret}, {"name": "publish", "status": "skipped"}]}), encoding="utf-8")
    (tmp_path / "licenses.txt").write_text(f"ok  numpy 2.5.3 — BSD-3-Clause\nNO  bad 1.0 — {secret}\n"
                                           "소스 조건: 소스를 같이 줘야 하는 배포판 2, 바퀴 안에 든 그런 구성요소 0\n", encoding="utf-8")
    (tmp_path / "v2.json").write_text(json.dumps({"days": 3, "normal": {"fuel_log": {"loaded": 3, "pages": 3, "oracle_cer": 0.0,
                                                                                      "presence_recall": 0.9846}}}), encoding="utf-8")
    assert main(["test 3.12", "job=test", "--junit", str(tmp_path / "junit.xml"), "--selftest", str(tmp_path / "selftest.json"),
                 "--licenses", str(tmp_path / "licenses.txt"), "--v2", str(tmp_path / "v2.json")]) == 0
    line = capsys.readouterr().out.strip()
    assert line.isascii() and str(tmp_path) not in line and "ALPHA" not in line
    for kv in ("job=test", "passed=7", "skipped=2", "failed=1", "seconds=13", "selftest=1", "selftest_s=40.9", "checks_passed=1",
               "checks_skipped=1", "licenses_ok=1", "licenses_no=1", "source_dists=2", "source_components=0",
               "normal_fuel_loaded=3of3", "normal_fuel_cer=0", "normal_fuel_recall=0.9846", "rough_env_cer=-"):
        assert kv in line.split("::", 2)[2].split(" "), kv
    assert main(["missing", "--junit", str(tmp_path / "없다.xml"), "--licenses", str(tmp_path / "없다.txt")]) == 0
    assert capsys.readouterr().out.strip() == "::notice title=missing::passed=- skipped=- failed=- errors=- seconds=- licenses_ok=- licenses_no=-"
