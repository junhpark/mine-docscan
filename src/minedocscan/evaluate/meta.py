"""쪽 메타의 평가 (eval --meta, tasks/0004 단계 5): 기계가 읽은 값을 사람·파일명의 값과 비교한다.

정답 = doc_page_meta 에서 출처가 review | label | filename 인 값 (날짜의 부분은 쪽의 날짜). 기계가 채운 값은 정답이 아니다 —
그래서 모델을 쓰기 시작한 뒤의 정답은 `review serve --queue page-fields --audit N` 의 표본에서 나온다 (4.6).

키마다:
  · 기계 값의 정확도 (기계가 읽은 쪽 중 — 잉크가 없어 읽지 않은 쪽은 틀린 것으로 센다)
  · 자동 적재율, 자동 적재 오류율 (분자·분모·윌슨 구간), 목록에 없는 값으로 답한 수
  · 배차가 바뀐 쪽만의 정확도 — 그 작성자의 가장 흔한 차(장비)가 아닌 차를 탄 쪽 (사람 값으로 정한다). 차량번호를 글씨체로
    외운 모델은 여기서 틀린다
그리고 일보에서: 기계 값만으로 정한 자리가 사람 값으로 정한 자리와 같은 쪽의 비율 (validate/crosscheck.assign_slots 그대로).
값(이름·차량번호)은 내지 않는다 — 수만.
"""
from __future__ import annotations

import sqlite3
from collections import Counter

from ..validate.crosscheck import assign_slots
from .stats import rate_with_ci

HUMAN = ("review", "label", "filename")


def evaluate_meta(con: sqlite3.Connection, split: str = "all", site=None) -> dict:
    if split != "all" and site is None:
        raise ValueError("--split 에는 사이트 팩이 필요합니다")
    rows = [dict(r) for r in con.execute(
        "SELECT m.*, p.work_date FROM doc_page_meta m JOIN doc_page p ON m.page_id = p.page_id "
        "WHERE m.machine_status IS NOT NULL ORDER BY m.page_id, m.meta_key")]
    if split != "all":
        rows = [r for r in rows if site.split_of(r["work_date"]) == split]
    human = {(r["page_id"], r["meta_key"]): r["value"] for r in con.execute(
        f"SELECT page_id, meta_key, value FROM doc_page_meta WHERE value IS NOT NULL AND source IN ({','.join('?' * len(HUMAN))})",
        HUMAN)}
    changed = _changed_pages(human)
    keys: dict[str, dict] = {}
    for k in sorted({r["meta_key"] for r in rows}):
        rs = [r for r in rows if r["meta_key"] == k and (r["page_id"], k) in human]
        ok = [r["machine_value"] == human[(r["page_id"], k)] for r in rs]
        auto = [r for r in rs if r["machine_status"] == "auto"]
        wrong = sum(r["machine_value"] != human[(r["page_id"], k)] for r in auto)
        ch = [(r, o) for r, o in zip(rs, ok, strict=True) if r["page_id"] in changed]
        keys[k] = {"n": len(rs), "correct": sum(ok), "accuracy": round(sum(ok) / len(rs), 4) if rs else None,
                   "auto_rate": round(len(auto) / len(rs), 4) if rs else None,
                   "auto_error": rate_with_ci(wrong, len(auto)) | {"auto": len(auto), "wrong": wrong},
                   "unlisted": sum(r["machine_status"] == "unlisted" for r in rs),
                   "machine": dict(sorted(Counter(r["machine_status"] for r in rs).items())),
                   "read_without_truth": sum((r["page_id"], k) not in human for r in rows if r["meta_key"] == k),
                   "changed": {"n": len(ch), "correct": sum(o for _r, o in ch),
                               "accuracy": round(sum(o for _r, o in ch) / len(ch), 4) if ch else None}}
    return {"split": split, "keys": keys, "slots": slot_agreement(con, split, site)}


WRITER = "operator"                       # 쪽을 쓰는 사람의 키 — 다른 키(차량번호·장비명 …)의 "평소 값"을 이 사람마다 센다
NOT_PAIRED = ("date", "date.month", "date.day")


def _changed_pages(human: dict) -> set[str]:
    """배차가 바뀐 쪽: 사람 값으로 본 그 작성자의 가장 흔한 값이 아닌 값을 적은 쪽 — 작성자와 짝이 되는 키(차량번호, 장비명 …)마다.
    한 키라도 평소와 다르면 바뀐 쪽이다. 키 이름을 고르지 않는다: 작성자·날짜가 아닌 키는 전부 짝이다."""
    writer = {p: v for (p, k), v in human.items() if k == WRITER and v}
    keys = {k for (_p, k) in human if k != WRITER and k not in NOT_PAIRED}
    changed: set[str] = set()
    for key in sorted(keys):
        pairs = {p: (writer[p], v) for (p, k), v in human.items() if k == key and v and p in writer}
        usual: dict[str, Counter] = {}
        for op, val in pairs.values():
            usual.setdefault(op, Counter())[val] += 1
        changed |= {p for p, (op, val) in pairs.items() if usual[op].most_common(1)[0][0] != val}
    return changed


def slot_agreement(con: sqlite3.Connection, split: str = "all", site=None) -> dict:
    """일보 쪽마다 자리: 기계 값(자동 적재된 것만)만으로 정한 자리 vs 사람 값만으로 정한 자리. 사람 값으로 자리가 정해진 쪽이 분모."""
    meta: dict[str, dict] = {}
    for pid, k, val, src, mval, ms in con.execute(
            "SELECT page_id, meta_key, value, source, machine_value, machine_status FROM doc_page_meta "
            "WHERE meta_key IN ('operator', 'vehicle_no')"):
        d = meta.setdefault(pid, {"human": {}, "machine": {}})
        if src in HUMAN and val is not None:
            d["human"][k] = val
        if ms == "auto":
            d["machine"][k] = mval
    same = total = machine_resolved = 0
    for (date,) in con.execute("SELECT DISTINCT work_date FROM prod_haul WHERE work_date IS NOT NULL ORDER BY 1").fetchall():
        if split != "all" and site.split_of(date) != split:
            continue
        rows = con.execute("SELECT * FROM prod_haul WHERE work_date = ?", (date,)).fetchall()
        slots: dict = {}
        for r in rows:
            if r["source_role"] == "matrix":
                slots.setdefault(r["slot"], (r["operator"], r["vehicle_no"]))
        pids = list(dict.fromkeys(r["page_id"] for r in rows if r["source_role"] == "log"))

        def pages_of(which: str, _pids=pids) -> dict:
            out = {}
            for p in _pids:
                d = meta.get(p, {}).get(which, {})
                out[p] = (d.get("vehicle_no"), d.get("operator"))
            return out

        by_human = assign_slots(pages_of("human"), slots)
        by_machine = assign_slots(pages_of("machine"), slots)
        machine_resolved += len(by_machine)
        for p, (slot, _how) in by_human.items():
            total += 1
            same += by_machine.get(p, (None,))[0] == slot
    return {"pages": total, "same": same, "rate": round(same / total, 4) if total else None,
            "machine_resolved": machine_resolved}
