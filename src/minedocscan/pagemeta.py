"""쪽 메타 — 날짜·차량번호·작성자 … 의 값이 어디서 왔는가 (tasks/0004 4.3, 4.4, 원칙 3).

우선순위: 검수값 > 페이지 라벨 > 문서 라벨 > 파일명 규칙 > 기계가 읽은 값(자동 적재 기준을 넘은 것만).
날짜는 결정 기록이 맨 앞이다: 쪽의 결정 > 문서의 결정 > 라벨(쪽 > 문서) > 파일명 규칙 (tasks/0007 4.2 — page_date 하나가 정한다).

  · 위에 값이 없으면 기계 값이 그 자리를 채운다. 기준을 못 넘었거나 목록에 없는 값이면 비어 있고 page-fields 대기열에 나온다.
  · 위에 값이 있으면 기계 값은 **대조에만** 쓴다(check_result). 다르면 다르다고 표시할 뿐 고치지 않는다 (ADR 0006).
  · 날짜는 언제나 결정·라벨·파일명에서 온다. 날짜의 부분(date.month, date.day)의 값은 그 날짜에서 나오고, 기계가 읽은 월·일은
    대조만 된다 — 읽은 부분이 **전부** 기준을 넘었는데 쪽의 날짜와 다르면 mismatch. 날짜를 읽은 값으로 정하지 않는다.

결과는 doc_page_meta 에 쪽 × 키마다 한 행으로 남는다. 기계 열(machine_*)은 파이프라인이 읽을 때만 정해지고 검수가 건드리지
않는다 — 검수를 저장하면 refresh_page() 가 기계 열을 그대로 두고 최종 값·출처·대조만 다시 계산한다. 그래서 저장 직후의 DB 와
같은 검수 파일로 새로 돌린 DB 가 같다 (불변식).

값(이름·차량번호)은 로그·오류 메시지에 찍지 않는다.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .forms.template import DATE_PARTS, Template
from .intake.dates import iso_date
from .review.store import meta_from_reviews
from .store.db import upsert

ISO_DATE = re.compile(r"^(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$")

# 사람(과 파일명)에서 온 값의 출처 — 이 목록 하나를 가져다 쓴다 (review/queue.py, review/export.py, evaluate/meta.py).
# decision: 결정 기록으로 정한 날짜 (tasks/0007 4.2). 그 쪽의 date.month·date.day 도 이 출처가 된다
HUMAN_SOURCES = ("review", "decision", "label", "filename")
MACHINE_STATUSES = ("auto", "pending", "unlisted", "empty")
CHECKS = ("match", "mismatch", "unread", "none")


@dataclass
class MachineRead:
    """기계가 메타 필드 하나를 읽은 결과. status: auto(기준 넘음, 목록 안) | pending | unlisted(목록에 없는 값) | empty(잉크 없음)."""

    value: str | None
    confidence: float | None
    status: str

    def __post_init__(self) -> None:
        if self.status not in MACHINE_STATUSES:
            raise ValueError(f"기계 상태는 {MACHINE_STATUSES} 중 하나: {self.status!r}")


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def page_date(site, source_name: str, page_no: int, decided: str | None = None) -> tuple[str | None, str | None]:
    """쪽의 날짜와 그 출처 (tasks/0007 4.2) — 분류의 후보, doc_page.work_date, doc_page_meta 의 date 가 다 이것 하나를 쓴다.
    순서: 결정(decided — 쪽의 결정 > 문서의 결정, intake.decisions.DocDecisions.page_date) > 쪽 라벨 > 문서 라벨 > 파일명 규칙.
    라벨의 값은 지금처럼 다듬어(_clean) 그대로 쓴다 — ISO 가 아닌 라벨 날짜면 쪽 메타가 월·일을 대조하지 않는다 (human_values)."""
    if decided:
        return decided, "decision"
    for lab in (site.labels.get(f"{source_name}#{page_no}", {}), site.labels.get(source_name, {})):
        v = _clean(lab.get("date"))
        if v is not None:
            return v, "label"
    d = site.date_from_filename(source_name)
    return (d, "filename") if d else (None, None)


def document_date(site, source_name: str, decided: str | None = None) -> tuple[str | None, str | None]:
    """문서의 날짜와 그 출처 (tasks/0007 4.2): 문서의 결정 > 문서 라벨 > 파일명 규칙 — 달력에 있는 ISO 날짜만 친다 (ISO 가
    아닌 라벨, 달력에 없는 파일명 날짜는 날짜가 아니다). 없으면 (None, None) — 그 문서는 needs_date 로 기다린다.
    쪽의 날짜만 있는 문서도 기다린다 (쪽 라벨·쪽의 결정은 문서의 날짜가 아니다)."""
    if decided and iso_date(decided):
        return decided, "decision"
    v = iso_date(_clean(site.labels.get(source_name, {}).get("date")))
    if v:
        return v, "label"
    v = iso_date(site.date_from_filename(source_name))
    return (v, "filename") if v else (None, None)


def human_values(con: sqlite3.Connection, site, source_name: str, page_no: int, page_id: str,
                 template: Template | None, decided: str | None = None) -> dict[str, tuple[str, str]]:
    """사람·파일명에서 온 값: {키: (값, 출처)}. 출처는 review | decision | label | filename. 날짜의 부분은 날짜에서 만든다.
    decided: 결정 기록으로 정한 그 쪽의 날짜 (쪽의 결정 > 문서의 결정) — 날짜는 page_date 의 순서.
    검수에서 빈 칸이면 (None, "review") — 라벨의 값을 지우고, 기계 값이 그 자리를 채우지도 못한다 (사람의 답이 이긴다)."""
    out: dict[str, tuple[str, str]] = {}
    for lab in (site.labels.get(source_name, {}), site.labels.get(f"{source_name}#{page_no}", {})):
        for k, v in lab.items():
            if _clean(v) is not None and k != "date":
                out[k] = (_clean(v), "label")
    d, src = page_date(site, source_name, page_no, decided)
    if d is not None:
        out["date"] = (d, src)
    if template is not None:
        for k, v in meta_from_reviews(con, page_id, template).items():
            if v is None:                                   # 검수에서 빈 칸: 라벨의 값을 지우고 기계 값도 막는다
                out[k] = (None, "review")
            elif _clean(v) is not None:
                out[k] = (_clean(v), "review")
    keys = set(template.meta_fields().values()) if template is not None else set()
    parts = ISO_DATE.match(out["date"][0] or "") if "date" in out and keys & set(DATE_PARTS) else None
    if parts:                                               # ISO 가 아닌 날짜(라벨의 오타 등)면 월·일은 대조하지 않는다
        src = out["date"][1]
        if "date.month" in keys:
            out["date.month"] = (str(int(parts["m"])), src)
        if "date.day" in keys:
            out["date.day"] = (str(int(parts["d"])), src)
    return out


def meta_keys(template: Template | None, human: dict) -> list[str]:
    """이 쪽에 행을 남길 키: date, 템플릿의 meta_key(필드 순서), 라벨·검수에만 있는 키."""
    keys = ["date"]
    if template is not None:
        keys += [k for k in template.meta_fields().values() if k not in keys]
    keys += sorted(k for k in human if k not in keys)
    return keys


def _check(human: tuple[str, str] | None, m: MachineRead | None) -> str:
    if m is None or human is None:
        return "none"
    if m.status in ("auto", "unlisted"):
        return "match" if m.value == human[0] else "mismatch"
    return "unread"


def _date_check(human: dict, machine: dict[str, MachineRead], keys: list[str]) -> str:
    """쪽의 날짜와 읽은 월·일의 대조: 자동 적재된 부분 하나라도 다르면 mismatch (다른 날의 쪽 — 다른 부분은 못 읽었어도),
    읽은 부분이 전부 자동 적재이고 같으면 match, 그 밖은 unread. 월 "1" 처럼 괘선 제거에 획이 다 지워져 읽지 못한 부분이 있어도
    일이 다르면 섞인 쪽으로 드러난다."""
    parts = [k for k in DATE_PARTS if k in keys and k in machine]
    if not parts or "date" not in human or not all(k in human for k in parts):     # 날짜가 ISO 가 아니면 부분이 없다
        return "none"
    auto = [k for k in parts if machine[k].status == "auto"]
    if any(machine[k].value != human[k][0] for k in auto):
        return "mismatch"
    return "match" if len(auto) == len(parts) else "unread"


def resolve(page_id: str, template: Template | None, human: dict[str, tuple[str, str]],
            machine: dict[str, MachineRead] | None = None) -> list[dict]:
    """한 쪽의 doc_page_meta 행들. machine: 읽는 모델이 있는 키만 (없는 키는 기계 열이 NULL)."""
    machine = machine or {}
    keys = meta_keys(template, human)
    field_of = {k: f"{page_id}:fields:{n}:-1" for n, k in (template.meta_fields() if template else {}).items()}
    date_check = _date_check(human, machine, keys)
    rows = []
    for k in keys:
        h, m = human.get(k), machine.get(k)
        if h is not None:
            value, source = h
        elif m is not None and m.status == "auto" and k not in DATE_PARTS and k != "date":
            value, source = m.value, "machine"
        else:
            value, source = None, None
        check = date_check if (k in DATE_PARTS or k == "date") else _check(h, m)
        rows.append({"page_id": page_id, "meta_key": k, "value": value, "source": source, "field_id": field_of.get(k),
                     "machine_value": None if m is None else m.value,
                     "machine_confidence": None if m is None else m.confidence,
                     "machine_status": None if m is None else m.status, "check_result": check})
    return rows


def write(con: sqlite3.Connection, page_id: str, rows: list[dict]) -> None:
    """그 쪽의 행을 바꿔 쓴다 (전에 있던 키가 없어졌으면 지운다)."""
    con.execute("DELETE FROM doc_page_meta WHERE page_id = ?", (page_id,))
    upsert(con, "doc_page_meta", rows)


def final_meta(rows: list[dict]) -> dict:
    """행들 → {키: 최종 값} (값이 없는 키는 빠진다). 핸들러가 쓰는 쪽 메타."""
    return {r["meta_key"]: r["value"] for r in rows if r["value"] is not None}


def page_meta_of(con: sqlite3.Connection, page_id: str) -> dict:
    """DB 에 적힌 그 쪽의 최종 메타."""
    return {r[0]: r[1] for r in con.execute("SELECT meta_key, value FROM doc_page_meta WHERE page_id = ? "
                                            "AND value IS NOT NULL", (page_id,))}


def machine_of(con: sqlite3.Connection, page_id: str) -> dict[str, MachineRead]:
    """그 쪽에서 기계가 읽어 둔 값 (파이프라인이 적은 기계 열)."""
    return {r[0]: MachineRead(r[1], r[2], r[3]) for r in con.execute(
        "SELECT meta_key, machine_value, machine_confidence, machine_status FROM doc_page_meta "
        "WHERE page_id = ? AND machine_status IS NOT NULL", (page_id,))}


def refresh_page(con: sqlite3.Connection, site, page_id: str) -> dict | None:
    """검수를 저장한 직후: 기계 열은 그대로 두고 최종 값·출처·대조를 다시 계산해 적는다. 돌려주는 값: 최종 메타."""
    from .intake.decisions import decided_date

    pg = con.execute("SELECT p.page_no, p.template_name, d.source_name FROM doc_page p JOIN doc_document d "
                     "ON p.document_id = d.document_id WHERE p.page_id = ?", (page_id,)).fetchone()
    if pg is None:
        return None
    tpl = site.templates.get(pg["template_name"]) if pg["template_name"] else None
    # 결정으로 정한 날짜를 잊지 않는다 (tasks/0007 4.2) — 차량번호를 검수한 뒤에도 그 쪽의 날짜는 결정의 것
    human = human_values(con, site, pg["source_name"], pg["page_no"], page_id, tpl, decided_date(con, page_id))
    rows = resolve(page_id, tpl, human, machine_of(con, page_id))
    write(con, page_id, rows)
    return final_meta(rows)


def is_meta_field(template: Template | None, field_name: str) -> bool:
    return template is not None and field_name in template.meta_fields()
