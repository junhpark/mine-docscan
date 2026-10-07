"""날짜 — 사람이 넣는 날짜를 읽는 곳 하나, 문서·라벨·파일명의 날짜가 달력에 있는지 (tasks/0007 4.2).

사람이 넣는 날짜(화면·`doc date`)는 여기서만 읽는다. 받는 표기:
  2025-03-26 · 2025.03.26 · 20250326     해까지
  25-03-26 · 25.03.26 · 25/03/26 · 250326   두 자리 해 (20xx)
  03-26 · 03.26 · 0326                   해 없이 — 받은 날을 넘지 않는 가장 가까운 해
달력에 없는 날짜만 거절한다 (DateError). 받은 날보다 뒤이거나 31일 넘게 앞이면 warning() 이 한 줄을 돌려준다 — 화면은 되묻고,
명령은 경고를 내고 저장한다 (지난 묶음을 넣는 일은 있고, 합성 묶음의 날짜는 2030년이다). "받은 날"은 인자로 받는다 — 시계에 기대지 않는다.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

FAR_DAYS = 31            # 받은 날보다 이만큼 넘게 앞이면 되묻는다 (한 달 치 묶음을 모아 넣는 일은 있다 — 그보다 앞은 드물다)


class DateError(ValueError):
    """날짜로 읽을 수 없다 (한 줄)."""


_FULL = re.compile(r"(?P<y>\d{4})(?P<s>[-./])(?P<m>\d{1,2})(?P=s)(?P<d>\d{1,2})")
_EIGHT = re.compile(r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})")
_SHORT = re.compile(r"(?P<y>\d{2})(?P<s>[-./])(?P<m>\d{1,2})(?P=s)(?P<d>\d{1,2})")
_SIX = re.compile(r"(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})")
_DAY = re.compile(r"(?P<m>\d{1,2})[-./](?P<d>\d{1,2})|(?P<m4>\d{2})(?P<d4>\d{2})")


def parse_date(text: str, received: date) -> str:
    """사람이 넣은 날짜 → ISO (YYYY-MM-DD). received: 그 문서를 받은 날 (해가 없는 표기의 해를 정한다)."""
    s = str(text or "").strip()
    for pat, century in ((_FULL, 0), (_EIGHT, 0), (_SHORT, 2000), (_SIX, 2000)):
        m = pat.fullmatch(s)
        if m:
            return _make(int(m["y"]) + century, int(m["m"]), int(m["d"]), s)
    m = _DAY.fullmatch(s)
    if m:
        mm, dd = int(m["m"] or m["m4"]), int(m["d"] or m["d4"])
        for y in range(received.year, received.year - 9, -1):      # 2월 29일은 윤년까지 거슬러 간다
            try:
                d = date(y, mm, dd)
            except ValueError:
                if not 1 <= mm <= 12 or not 1 <= dd <= 31:
                    break
                continue
            if d <= received:
                return d.isoformat()
        raise DateError(f"달력에 없는 날짜입니다: {s}")
    raise DateError(f"날짜로 읽을 수 없습니다: {s!r} (예: 2025-03-26, 25.03.26, 250326, 0326)")


def _make(y: int, m: int, d: int, s: str) -> str:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        raise DateError(f"달력에 없는 날짜입니다: {s}") from None


def warning(iso: str, received: date) -> str | None:
    """받은 날보다 뒤이거나 FAR_DAYS 일 넘게 앞이면 한 줄 (값은 날짜뿐 — 이름이 없다). 아니면 None."""
    d = date.fromisoformat(iso)
    if d > received:
        return f"받은 날({received.isoformat()})보다 뒤의 날짜입니다: {iso}"
    if d < received - timedelta(days=FAR_DAYS):
        return f"받은 날({received.isoformat()})보다 {FAR_DAYS}일 넘게 앞의 날짜입니다: {iso}"
    return None


def iso_date(value) -> str | None:
    """달력에 있는 ISO 날짜(YYYY-MM-DD)면 그 글자열, 아니면 None — 문서의 날짜는 이것만 친다 (라벨·파일명·결정)."""
    s = str(value or "").strip()
    if len(s) != 10:
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    return d.isoformat() if d.isoformat() == s else None
