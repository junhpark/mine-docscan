"""CI 의 알림(annotation) 한 줄 (tasks/0010 4.6) — 검증하는 쪽은 CI 의 산출물·기록을 받지 못하고 단계별 성공/실패와 알림만 본다.
그래서 보고할 수치를 알림으로도 남긴다. 알림 글은 이 한 곳에서 만든다.

- **ASCII 만** — 윈도우 러너의 표준 출력은 cp1252 이고 run 블록도 ASCII 만 (tasks/0009). `key=value` 꼴.
- **수만** — 값은 글자·숫자·`._+-` 만 받는다 (그 밖이면 ValueError): 경로(`/`·`\\`·`:`)·빈칸이 든 이름이 들어갈 자리가 없다.
- GitHub 의 규칙대로 이스케이프 — 글의 `%`·CR·LF (여러 줄은 `%0A` 로 한 알림에), 제목은 `:`·`,` 도.
- 한 단계에 같은 종류 10개, 한 작업에 50개가 GitHub 의 상한 — 부르는 쪽이 작업마다 한두 개만 낸다.

  python scripts/ci_notice.py "windows tests" passed=571 skipped=26 failed=0 seconds=712     # 한 줄을 찍는다
"""
from __future__ import annotations

import re
import sys
from collections.abc import Iterable

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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    title, rest = argv[0], argv[1:]
    items = [tuple(a.split("=", 1)) for a in rest if "=" in a]
    print(notice(title, items), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
