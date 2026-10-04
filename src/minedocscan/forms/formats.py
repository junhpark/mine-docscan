"""값의 형식 (tasks/0005 4.1): 손으로 쓰는 칸·필드의 `format` 과, 입력을 DB 에 남길 표기로 바꾸는 규칙 하나.

  format       적는 것                 DB 의 표기 (정규화)
  integer      정수                    7          (앞의 0 을 뗀다 — handwritten_number 의 기본)
  decimal      소수                    1234.5     (앞의 0 을 떼고, 쓴 소수 자릿수는 그대로)
  time         시각                    08:00      (24시간, 두 자리씩)
  time_range   시각 범위               08:00~17:00 (끝이 시작보다 이르면 다음 날 — 연근)
  reading      계기 값 또는 시각       숫자면 decimal 처럼, 콜론이 있으면 time 처럼
  (없음)       글자                    그대로 (앞뒤 빈칸만 뗀다 — handwritten_text 의 기본)

계기 칸의 규칙 (검수 화면의 안내, docs/DATA.md 와 같은 문장): 계기 값은 숫자 그대로(1234.5), 시각은 콜론으로(08:00).
종이에 점으로 쓴 시각(08.00)도 시각이면 콜론으로 입력한다. 숫자인지 시각인지는 사람이 정한다 — 코드는 콜론만 본다.

검수 저장(review/store.save)·검수 서버·정답 내보내기·평가·핸들러가 전부 이 모듈을 쓴다. 형식에 맞지 않는 입력은 FormatError —
검수 파일에 남기 전에 거절한다.
"""
from __future__ import annotations

import re

FORMATS = ("integer", "decimal", "time", "time_range", "reading")
NUMBER_KINDS = ("handwritten_number",)
FORMAT_KINDS = ("handwritten_number", "handwritten_text")      # format 을 줄 수 있는 칸 종류

# 검수 화면이 형식마다 받는 글자와 안내 (index.html 이 그대로 쓴다). 서버가 다시 검사한다 — 화면은 편의일 뿐이다
# 정규식의 글자 묶음([...]) 안에 그대로 넣는다 — 붙임표는 이스케이프 (끝이 아니면 범위가 된다)
INPUT_CHARS = {"integer": "0-9", "decimal": "0-9.", "time": "0-9:.", "time_range": "0-9:.~\\-", "reading": "0-9.:"}
HINTS = {
    "integer": "정수 (예: 7)",
    "decimal": "소수 (예: 1234.5)",
    "time": "시각 (예: 08:00, 8:00, 0800)",
    "time_range": "시각 범위 (예: 08:00~17:00, 8-17)",
    "reading": "계기 값은 숫자 그대로(1234.5), 시각은 콜론으로(08:00) — 점으로 쓴 시각(08.00)도 콜론으로 입력",
}

_DECIMAL = re.compile(r"^(\d+)(?:\.(\d+))?$")
_HHMM = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_RANGE_SEP = re.compile(r"\s*[~-]\s*")


# 한글 입력기가 낼 수 있는 전각 글자 → ASCII (숫자, 콜론, 점, 물결, 붙임표)
_FULLWIDTH = str.maketrans({**{chr(0xFF10 + i): str(i) for i in range(10)}, "：": ":", "．": ".", "～": "~", "〜": "~",
                            "－": "-", "–": "-", "—": "-"})


class FormatError(ValueError):
    """입력이 칸의 형식에 맞지 않는다. 메시지 한 줄에 무엇이 틀렸는지 (값은 칸의 값 — 이름·차량번호 칸에는 형식이 없다)."""


def default_format(kind: str) -> str | None:
    """칸 종류의 기본 형식: 숫자 칸은 integer, 글자 칸은 없음 (지금과 같다)."""
    return "integer" if kind in NUMBER_KINDS else None


def normalize(fmt: str | None, text: str) -> str:
    """입력 → DB 에 남길 표기. 맞지 않으면 FormatError. fmt 가 None 이면 글자 — 앞뒤 빈칸만 뗀다."""
    t = "" if text is None else str(text).strip()
    if fmt is None:
        return t
    t = t.translate(_FULLWIDTH).replace(" ", "")
    if fmt not in FORMATS:
        raise FormatError(f"알 수 없는 형식: {fmt!r} (가능: {', '.join(FORMATS)})")
    if t == "":
        raise FormatError("값이 비었습니다 (빈 칸이면 빈 칸으로 저장)")
    if fmt == "integer":
        if not t.isdigit() or not t.isascii():
            raise FormatError(f"정수만: {t!r}")
        return str(int(t))
    if fmt == "decimal":
        return _decimal(t)
    if fmt == "time":
        return _time(t, dots=True)
    if fmt == "time_range":
        a, b = _range(t)
        return f"{a}~{b}"
    # reading: 콜론이 있으면 시각, 없으면 계기 값 (점은 소수점이다 — 사람이 콜론으로 가른다)
    return _time(t, dots=False) if ":" in t else _decimal(t)


def is_valid(fmt: str | None, text: str) -> bool:
    try:
        normalize(fmt, text)
    except FormatError:
        return False
    return True


def try_normalize(fmt: str | None, text: str | None) -> str | None:
    """비교용: 정규화한 표기, 맞지 않으면 원래 문자열 (평가에서 기계의 엉뚱한 답을 그대로 틀린 값으로 남기려고). None 은 None."""
    if text is None:
        return None
    try:
        return normalize(fmt, text)
    except FormatError:
        return str(text).strip()


# ── 값으로 ─────────────────────────────────────────────────────────────────
def reading_kind(text: str | None) -> str | None:
    """정규화된 reading 표기의 종류: meter(계기 값) | clock(시각) | None(비었다)."""
    if not text:
        return None
    return "clock" if ":" in text else "meter"


def as_number(text: str | None) -> float | None:
    """정규화된 decimal/integer/reading(계기) 표기 → 수. 시각·빈 값·숫자가 아닌 것은 None."""
    if not text or ":" in text:
        return None
    m = _DECIMAL.match(text)
    return float(text) if m else None


def minutes(text: str | None) -> int | None:
    """정규화된 시각 표기(HH:MM) → 0시부터의 분. 아니면 None."""
    if not text:
        return None
    m = re.match(r"^(\d{2}):(\d{2})$", text)
    return int(m[1]) * 60 + int(m[2]) if m else None


def range_minutes(text: str | None) -> int | None:
    """정규화된 시각 범위(HH:MM~HH:MM) → 길이(분). 끝이 시작보다 이르면 다음 날로 본다 (연근). 아니면 None."""
    if not text or "~" not in text:
        return None
    a, b = (minutes(x) for x in text.split("~", 1))
    if a is None or b is None:
        return None
    return b - a if b >= a else b + 24 * 60 - a


def span_minutes(start: str | None, end: str | None) -> int | None:
    """두 시각(HH:MM) 사이의 분. 끝이 시작보다 이르면 다음 날."""
    a, b = minutes(start), minutes(end)
    if a is None or b is None:
        return None
    return b - a if b >= a else b + 24 * 60 - a


# ── 안쪽 ───────────────────────────────────────────────────────────────────
def _decimal(t: str) -> str:
    m = _DECIMAL.match(t)
    if not m or not t.isascii():
        raise FormatError(f"숫자(소수)만: {t!r} — 예: 1234.5")
    whole, frac = m[1], m[2]
    return f"{int(whole)}" + (f".{frac}" if frac is not None else "")


def _time(t: str, dots: bool) -> str:
    """시각 하나: 8:00, 08:00, 0800, 800, 8 → 08:00. dots 면 08.00 도 시각 (time 형식 — reading 에서는 점은 소수점)."""
    if not t.isascii():
        raise FormatError(f"시각: {t!r} — 예: 08:00")
    m = _HHMM.match(t)
    if m and (dots or ":" in t):
        h, mi = int(m[1]), int(m[2])
    elif t.isdigit() and len(t) in (3, 4):                    # 0800, 800
        h, mi = int(t[:-2]), int(t[-2:])
    elif t.isdigit() and len(t) in (1, 2):                    # 8 → 08:00 (시각 범위의 8-17 처럼)
        h, mi = int(t), 0
    else:
        raise FormatError(f"시각: {t!r} — 예: 08:00 (점으로 쓴 시각도 콜론으로)")
    if mi >= 60:
        raise FormatError(f"분은 59 까지: {t!r}")
    if h > 24 or (h == 24 and mi > 0):
        raise FormatError(f"24:00 을 넘는 시각: {t!r}")
    return f"{h:02d}:{mi:02d}"


def _range(t: str) -> tuple[str, str]:
    parts = _RANGE_SEP.split(t)
    if len(parts) != 2 or not all(parts):
        raise FormatError(f"시각 범위: {t!r} — 예: 08:00~17:00, 8-17")
    return _time(parts[0], dots=True), _time(parts[1], dots=True)
