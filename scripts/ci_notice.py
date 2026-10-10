"""CI 의 알림(annotation) 한 줄 (tasks/0010 4.6) — 검증하는 쪽은 CI 의 산출물·기록을 받지 못하고 단계별 성공/실패와 알림만 본다.
그래서 보고할 수치를 알림으로도 남긴다. 알림 글은 이 한 곳에서 만든다.

- **ASCII 만** — 윈도우 러너의 표준 출력은 cp1252 이고 run 블록도 ASCII 만 (tasks/0009). `key=value` 꼴.
- **수만** — 값은 글자·숫자·`._+-` 만 받는다 (그 밖이면 ValueError): 경로(`/`·`\\`·`:`)·빈칸이 든 이름이 들어갈 자리가 없다.
- GitHub 의 규칙대로 이스케이프 — 글의 `%`·CR·LF (여러 줄은 `%0A` 로 한 알림에), 제목은 `:`·`,` 도.
- 한 단계에 같은 종류 10개, 한 작업에 50개가 GitHub 의 상한 — 부르는 쪽이 작업마다 한두 개만 낸다.

  python scripts/ci_notice.py "windows tests" passed=571 skipped=26 failed=0 seconds=712     # 한 줄을 찍는다
  python scripts/ci_notice.py "test 3.12" --junit junit/junit.xml --selftest junit/selftest/selftest.json --licenses junit/licenses.txt
      파일에서 수만 읽어 더한다: --junit (통과·건너뜀·실패·오류·초), --selftest (통과·초·검사 수), --licenses (ok·NO·소스 조건의 수),
      --v2 (확장성 표의 V2 양식 — 보통·거친 글씨의 oracle CER·값 유무 재현율·적재). 파일이 없거나 읽을 수 없으면 그 값은 `-`.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

_KEY = re.compile(r"[a-z][a-z0-9_]{0,40}\Z")
_VALUE = re.compile(r"[A-Za-z0-9._+\-]{0,80}\Z")
_TITLE = re.compile(r"[A-Za-z0-9 ._\-]{1,60}\Z")


def fields(items: dict | Iterable[tuple[str, object]]) -> str:
    """key=value 를 빈칸으로 잇는다. 값: None → -, bool → 1/0, 실수 → 소수 넷째 자리까지."""
    out = []
    for k, v in (items.items() if isinstance(items, dict) else items):
        if v is None:
            v = "-"
        elif isinstance(v, bool):
            v = int(v)
        elif isinstance(v, float):
            v = f"{v:.4f}".rstrip("0").rstrip(".") if v == v else "nan"
        v = str(v)
        if not _KEY.match(k):
            raise ValueError(f"알림의 키는 영문 소문자·숫자·밑줄만: {k!r}")
        if not _VALUE.match(v):
            raise ValueError(f"알림의 값은 글자·숫자·._+- 만 (경로·이름이 들어가지 않게): {k}")
        out.append(f"{k}={v}")
    return " ".join(out)


def _escape_data(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def notice(title: str, *lines: dict | Iterable[tuple[str, object]] | str, level: str = "notice") -> str:
    """`::notice title=…::…` 한 줄. lines 가 여럿이면 한 알림에 여러 줄 (%0A). 줄은 fields() 의 꼴 또는 이미 만든 글자열
    (그것도 빈칸 하나로 띄운 key=value 만)."""
    if level not in ("notice", "warning", "error"):
        raise ValueError(level)
    if not _TITLE.match(title):
        raise ValueError("알림의 제목은 영문·숫자·빈칸·._- 만")
    text = []
    for line in lines:
        s = line if isinstance(line, str) else fields(line)
        for tok in s.split(" "):
            k, eq, v = tok.partition("=")
            if not (eq and _KEY.match(k) and _VALUE.match(v)):
                raise ValueError("알림의 줄은 빈칸 하나로 띄운 key=value 만 (값은 글자·숫자·._+-)")
        text.append(s)
    msg = _escape_data("\n".join(text))
    t = title.replace("%", "%25").replace(":", "%3A").replace(",", "%2C")
    return f"::{level} title={t}::{msg}"


def from_junit(path: Path) -> list[tuple[str, object]]:
    """JUnit XML → 통과·건너뜀·실패·오류·초 (시험의 이름·글은 읽지 않는다)."""
    import xml.etree.ElementTree as ET

    keys = ("passed", "skipped", "failed", "errors", "seconds")
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return [(k, None) for k in keys]
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    n = {k: sum(int(float(s.get(k) or 0)) for s in suites) for k in ("tests", "skipped", "failures", "errors")}
    secs = sum(float(s.get("time") or 0) for s in suites)
    return [("passed", n["tests"] - n["skipped"] - n["failures"] - n["errors"]), ("skipped", n["skipped"]),
            ("failed", n["failures"]), ("errors", n["errors"]), ("seconds", round(secs))]


def from_selftest(path: Path) -> list[tuple[str, object]]:
    """selftest.json → 통과(1/0)·초·검사의 수 (통과·실패·건너뜀)."""
    try:
        r = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [("selftest", None), ("selftest_s", None)]
    st = [c.get("status") for c in r.get("checks", [])]
    return [("selftest", bool(r.get("passed"))), ("selftest_s", r.get("seconds")), ("checks_passed", st.count("passed")),
            ("checks_failed", st.count("failed")), ("checks_skipped", st.count("skipped"))]


def from_licenses(path: Path) -> list[tuple[str, object]]:
    """licenses.py 가 찍은 줄 → ok·NO 의 수, 소스 조건 (배포판·구성요소의 수)."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [("licenses_ok", None), ("licenses_no", None)]
    m = next((re.search(r"(\d+)\D+(\d+)\s*$", x) for x in lines if x.startswith("소스 조건")), None)
    return [("licenses_ok", sum(x.startswith("ok ") for x in lines)), ("licenses_no", sum(x.startswith("NO ") for x in lines)),
            ("source_dists", m.group(1) if m else None), ("source_components", m.group(2) if m else None)]


def from_v2(path: Path) -> list[tuple[str, object]]:
    """v2_metrics.py 의 JSON → V2 양식(유류일지·환경일지)마다, 보통·거친 글씨의 적재/쪽·oracle CER·값 유무 재현율."""
    try:
        r = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [("v2", None)]
    out: list[tuple[str, object]] = [("days", r.get("days"))]
    for style in ("normal", "rough"):
        for form, short in (("fuel_log", "fuel"), ("env_log", "env")):
            m = (r.get(style) or {}).get(form) or {}
            out += [(f"{style}_{short}_loaded", f"{m.get('loaded', '-')}of{m.get('pages', '-')}"),
                    (f"{style}_{short}_cer", m.get("oracle_cer")), (f"{style}_{short}_recall", m.get("presence_recall"))]
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("title")
    ap.add_argument("fields", nargs="*", help="key=value")
    ap.add_argument("--junit", type=Path)
    ap.add_argument("--selftest", type=Path)
    ap.add_argument("--licenses", type=Path)
    ap.add_argument("--v2", type=Path)
    a = ap.parse_args(argv)
    items: list[tuple[str, object]] = [tuple(f.split("=", 1)) for f in a.fields if "=" in f]
    for path, read in ((a.junit, from_junit), (a.selftest, from_selftest), (a.licenses, from_licenses), (a.v2, from_v2)):
        if path is not None:
            items += read(path)
    print(notice(a.title, items), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
