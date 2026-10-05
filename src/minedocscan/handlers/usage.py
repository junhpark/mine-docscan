"""장비 가동 일보 핸들러 (tasks/0005 4.2, 4.3): 장비 한 대의 하루를 적은 일보 → eq_usage_daily (쪽 하나에 한 행) + prod_tally.

양식의 차이는 템플릿의 "표의 역할"(region role)로 흡수한다. 새 양식이 이 네 가지의 조합이면 코드를 고치지 않는다 (원칙 1).

  role: meter       계기 칸 — 시작·종료·총 (열 이름 또는 행 키 start, end, total). 형식 reading | decimal | time
  role: shifts      근무 시각 — 행(근무 구분) × 시각 범위(format time_range) → shifts JSON, 가동 분의 합
  role: tally       작업량 — 행 메타 item(·place) × 열(메타 shift) 의 정수 → prod_tally. 소계 칸은 열·행 메타 subtotal: true
  role: activities  작업 표의 글자 칸 — doc_field 에만. 글씨가 있는 줄의 수만 activity_rows 에

표 밖: 장비명(meta_key equipment), 운전자(meta_key operator) — 쪽 메타(doc_page_meta)의 최종 값을 쓴다. 서명(kind signature)은 잉크 유무.
연료·오일·특이사항 같은 글자 필드는 doc_field 에만.

값 유무:
  · meter·shifts·tally 표의 형식 있는 칸은 운반 칸과 같다 — 괘선 제거 + RLSA 덩어리 배정(imaging/blobs.py), 기준 면적은 운반
    핸들러의 MIN_BLOB_AREA 그대로. 칸보다 큰 글씨, 표 위에 걸친 메모(덩어리가 여러 칸에 걸치면 값이 아니다)를 같은 규칙으로 다룬다
  · 그 밖의 칸(작업 표, 필드)은 잉크 비율 (FormHandler 의 text_ink_min 그대로). 새 임계값을 만들지 않는다
값:
  · 정수 칸(작업량)은 지금의 숫자 인식 경로(by_kind, 자동 적재 표)를 그대로 탄다 — handlers/base.number_row
  · 소수·시각 칸은 읽지 않는다 (미룸 — tasks/0006 1절). 잉크가 있으면 검수 대기 — 계기 칸은 `review serve --queue readings`

가동 시간(hours)은 적힌 값에서 계산하고, 무엇으로 계산했는지를 hours_basis 에 남긴다 (4.3):
  meter(종료 − 시작) > total(총 칸) > clock(계기 칸에 적은 시각의 종료 − 시작) > shifts(근무 시각 범위의 합) > NULL.
  추정하지 않는다: 앞선 근거에 아직 모르는 칸(잉크는 있는데 값이 없다 — 검수 대기)이 있으면 그 뒤로 내려가지 않고 NULL,
  계기 칸에 계기 값과 시각이 섞였으면 NULL (tasks/0005 9절).

업무 행은 검수를 적용한 최종 필드 행에서 만든다 (usage_rows — load 와 on_review 가 같은 함수). 장비 ID 는 사이트 팩의 대응표로만.
검산(validate/usage.py → xcheck_usage): 쪽 안(총 = 종료 − 시작, 소계 = 합)은 적재할 때, 계기의 연속성은 마무리에서 전체를.
검수를 저장하면 그 쪽의 검산과 그 장비(이름이 바뀌었으면 예전·새 장비)의 연속성만 다시 계산한다.
"""
from __future__ import annotations

import json

from ..forms.equipment import META_KEY as EQUIPMENT
from ..forms.formats import as_number, minutes, range_minutes, reading_kind, span_minutes, try_normalize
from ..forms.template import Template, meter_slot
from ..imaging.blobs import assign_blobs
from ..pagemeta import page_meta_of
from ..store.db import upsert
from ..validate.usage import check_usage, equipment_ref, recompute_continuity, write_page_checks
from .base import (
    FormHandler,
    PageContext,
    apply_reviews,
    as_int,
    field_row,
    number_row,
    readable,
    recognize,
    unread_row,
)
from .haul import MIN_BLOB_AREA

BLOB_ROLES = ("meter", "shifts", "tally")       # 낮은 칸, 글씨가 칸을 넘는 표 — 운반 칸과 같은 덩어리 배정
UNKNOWN = object()                              # 잉크는 있는데 값을 모르는 칸 (검수 대기)


class UsageHandler(FormHandler):
    name = "usage"

    def load(self, ctx: PageContext) -> dict:
        tpl = ctx.template
        roles = {reg["name"]: reg.get("role") for reg in tpl.regions}

        # 덩어리 배정: meter·shifts·tally 표의 형식 있는 칸 (표마다)
        blob = [o for o in ctx.obs if roles.get(o.cell.region) in BLOB_ROLES and _formatted(o.cell)]
        area: dict[int, int] = {}
        n_notes = 0
        for reg in tpl.regions:
            cells = [o for o in blob if o.cell.region == reg["name"]]
            if not cells:
                continue
            ink_by, blobs = assign_blobs(ctx.aligned, [o.cell for o in cells], reg["grid"]["ys"], reg["grid"]["xs"])
            for ci, a in ink_by.items():
                area[id(cells[ci])] = a
            n_notes += int(sum(bool(b.is_note) for b in blobs))       # numpy bool → int (실행 요약은 int 만 더한다)
        blob_ids = {id(o) for o in blob}

        def inked(o) -> bool:
            return area.get(id(o), 0) >= MIN_BLOB_AREA if id(o) in blob_ids else o.ink >= self.text_ink_min

        def any_ink(o) -> bool:
            """두 잉크 판정(덩어리 배정, 잉크 비율) 중 하나라도 "있음"이면 "있음" — 빈 칸으로 자동 적재하지 않는다.
            덩어리 배정은 이웃 칸의 글씨와 한 덩어리로 묶인 글씨를 메모로 보고 그 칸들을 다 비었다고 한다:
              · 긴 계기 값(1234.5)이 이웃 칸의 값과 붙을 때
              · 작업량 칸 안에 인쇄된 라벨·단위("하단: _ 대")가 있을 때 — 쓴 숫자가 양옆의 인쇄와, 인쇄가 이웃 칸의 인쇄와 이어져
                줄 전체가 메모가 된다 (실제 로우더 작업일보에서 숫자를 쓴 칸 48개를 하나도 잡지 못했다)
            그 칸을 빈 칸으로 자동 적재하면 값이 조용히 사라진다. 잘못 "있음"이면 인식기가 빈 칸으로 답하거나 검수 한 번이 든다.
            새 임계값은 없다 (MIN_BLOB_AREA, text_ink_min)."""
            return inked(o) or o.ink >= self.text_ink_min

        # 표의 정수 칸(작업량): 숫자 인식 경로. 두 잉크 판정 중 하나라도 "있음"인 칸을 인식기에 — 운반 칸(haul)은 덩어리 배정만
        ints = [o for o in ctx.obs if o.cell.region != "fields" and o.cell.kind.startswith("handwritten")
                and o.cell.fmt == "integer"]
        to_read = [o for o in ints if any_ink(o)]
        recs = dict(zip([id(o) for o in to_read], recognize(ctx, to_read), strict=True))
        int_ids = {id(o) for o in ints}

        rows, hw = [], []
        for o in ctx.obs:
            c = o.cell
            if id(o) in int_ids:
                rows.append(number_row(ctx, o, recs.get(id(o))))
            elif c.kind.startswith("handwritten") and c.region != "fields" and not readable(c):
                # 소수·시각 칸: 읽지 않는다. 잉크가 있으면 검수 대기, 없으면 빈 칸 (value_final NULL — machine_final 과 같다)
                rows.append(unread_row(ctx, o) if any_ink(o) else number_row(ctx, o, None))
            elif c.kind == "printed":
                v = c.row_meta.get(c.name)
                rows.append(field_row(ctx, o, has_value=None, value_raw=None, value_final=None if v is None else str(v),
                                      confidence=1.0, candidates=None, backend="template", review_status="auto"))
            elif c.kind.startswith("handwritten"):
                hw.append(o)                                 # 글자 칸·필드: 기본 규칙 (잉크 비율 → 인식기, 읽지 않는 형식은 검수 대기)
            else:                                            # 체크·서명: 잉크 유무만
                rows.append(field_row(ctx, o, has_value=o.ink >= self.text_ink_min, value_raw=None, value_final=None,
                                      confidence=None, candidates=None, backend="ink", review_status="auto"))
        rows += self.load_handwritten(ctx, hw)
        rows = apply_reviews(ctx, rows)                      # 검수가 있으면 최종값으로 덮는다
        upsert(ctx.con, "doc_field", rows)

        page = {"page_id": ctx.page_id, "work_date": ctx.work_date, "template_name": tpl.name}
        usage, tally = usage_rows(ctx.site, tpl, page, ctx.meta, rows)
        write_page(ctx.con, usage, tally)
        write_page_checks(ctx.con, tpl, ctx.page_id)          # 쪽 안의 검산. 날짜 사이(연속성)는 마무리에서 전체를
        return {"fields": len(rows), "usage_pages": 1, "tally_cells": len(tally),
                "tally_filled": sum(t["has_value"] for t in tally), "notes": n_notes,
                "meter_pending": int(usage["reading_kind"] == "pending")}

    def finalize(self, con, site, settings) -> dict:
        """모든 쪽을 적재한 뒤: 검산 전부 (쪽 안 + 계기의 연속성)."""
        return {"xcheck_usage": check_usage(con, site)}

    def machine_final(self, row: dict) -> str | None:
        """기계만으로 정했을 때의 value_final (load 와 같은 규칙): 필드·글자 칸은 기계 값 그대로, 정수 칸은 정수로, 소수·시각 칸은 NULL."""
        if row["region"] == "fields" or row["format"] is None:
            return row["value_raw"]
        if row["format"] == "integer":
            v = as_int(row["value_raw"])
            return None if v is None else str(v)
        return None

    def on_review(self, con, site, settings, field_id: str) -> None:
        """검수 직후: 그 쪽의 eq_usage_daily·prod_tally 를 최종 필드 행에서 다시 만든다 (계기·작업량 칸, 장비명·운전자 필드 모두).
        쪽 메타는 store.save 가 먼저 다시 계산해 두었다 (refresh_page)."""
        f = con.execute("SELECT f.page_id, p.template_name, p.work_date FROM doc_field f JOIN doc_page p "
                        "ON f.page_id = p.page_id WHERE f.field_id = ?", (field_id,)).fetchone()
        if f is None or f["template_name"] not in site.templates:
            return
        tpl = site.templates[f["template_name"]]
        before = rebuild_page(con, site, tpl, {"page_id": f["page_id"], "work_date": f["work_date"],
                                               "template_name": f["template_name"]})
        after = con.execute("SELECT * FROM eq_usage_daily WHERE page_id = ?", (f["page_id"],)).fetchone()
        write_page_checks(con, tpl, f["page_id"])
        # 그 장비의 연속성 — 장비명이 바뀌었으면 예전 장비와 새 장비 둘 다 (앞뒤 기록의 검산이 바로 바뀐다)
        refs = {equipment_ref(after)} | ({equipment_ref(before)} if before is not None else set())
        recompute_continuity(con, refs, {f["page_id"]})


def rebuild_page(con, site, tpl: Template, page: dict) -> dict | None:
    """DB 의 최종 필드 행과 쪽 메타로 그 쪽의 업무 행을 다시 만든다. 돌려주는 값: 전에 있던 eq_usage_daily 행 (없었으면 None)."""
    before = con.execute("SELECT * FROM eq_usage_daily WHERE page_id = ?", (page["page_id"],)).fetchone()
    frows = [dict(r) for r in con.execute("SELECT * FROM doc_field WHERE page_id = ?", (page["page_id"],))]
    usage, tally = usage_rows(site, tpl, page, page_meta_of(con, page["page_id"]), frows)
    write_page(con, usage, tally)
    return None if before is None else dict(before)


def write_page(con, usage: dict, tally: list[dict]) -> None:
    upsert(con, "eq_usage_daily", usage)
    con.execute("DELETE FROM prod_tally WHERE page_id = ?", (usage["page_id"],))     # 템플릿이 바뀌어 칸이 줄었어도 남지 않게
    upsert(con, "prod_tally", tally)


# ── 최종 필드 행 → 업무 행 (load 와 on_review 가 같이 쓴다) ──────────────────────
def usage_rows(site, tpl: Template, page: dict, meta: dict, frows: list[dict]) -> tuple[dict, list[dict]]:
    """한 쪽의 (eq_usage_daily 행, prod_tally 행들). frows: 그 쪽의 최종 doc_field 행 (검수 적용 뒤)."""
    roles = {reg["name"]: reg.get("role") for reg in tpl.regions}
    by_region: dict[str, list[dict]] = {}
    for r in frows:
        by_region.setdefault(r["region"], []).append(r)
    equipment = meta.get(EQUIPMENT)
    eid = site.equipment_id_of(equipment)

    meter = {}                                               # 칸 이름(start·end·total) → 최종 필드 행
    for reg, role in roles.items():
        if role == "meter":
            for r in by_region.get(reg, []):
                slot = _meter_slot(r)
                if slot and r["kind"].startswith("handwritten"):
                    meter[slot] = r
    shift_rows = [r for reg, role in roles.items() if role == "shifts" for r in by_region.get(reg, [])
                  if r["kind"].startswith("handwritten") and r["format"] == "time_range"]

    vals = {s: _state(r) for s, r in meter.items()}
    kind, m = _reading(vals, meter, has_meter=any(role == "meter" for role in roles.values()))
    shifts, shift_min = _shifts(shift_rows)
    hours, basis = _hours(vals, kind, m, shift_min, shift_rows)
    statuses = [r["review_status"] for r in [*meter.values(), *shift_rows]]
    usage = {
        "page_id": page["page_id"], "work_date": page["work_date"], "source_form": tpl.name,
        "equipment": equipment, "equipment_id": eid, "operator": meta.get("operator"),
        "reading_kind": kind,
        "meter_start": m.get("meter", {}).get("start"), "meter_end": m.get("meter", {}).get("end"),
        "meter_total": m.get("meter", {}).get("total"),
        "clock_start": m.get("clock", {}).get("start"), "clock_end": m.get("clock", {}).get("end"),
        "meter_start_raw": _raw(meter.get("start")), "meter_end_raw": _raw(meter.get("end")),
        "meter_total_raw": _raw(meter.get("total")),
        "shifts": shifts, "shift_minutes": shift_min,
        "activity_rows": _activity_rows(tpl, roles, by_region), "signed": _signed(frows),
        "hours": hours, "hours_basis": basis,
        "start_field_id": _fid(meter.get("start")), "end_field_id": _fid(meter.get("end")),
        "total_field_id": _fid(meter.get("total")),
        "review_status": "pending" if "pending" in statuses else ("reviewed" if "reviewed" in statuses else "auto"),
    }
    tally = []
    for reg, role in roles.items():
        if role != "tally":
            continue
        treg = tpl.region(reg)
        trows = {r["row"]: r for r in treg["rows"]}
        tcols = {c["name"]: c for c in treg["columns"]}
        for r in sorted(by_region.get(reg, []), key=lambda x: (x["row_no"], x["x0"] or 0)):
            if r["format"] != "integer" or not r["kind"].startswith("handwritten"):
                continue
            row_meta, col_meta = trows.get(r["row_no"], {}), tcols.get(r["field_name"], {})
            tally.append({
                "tally_id": r["field_id"], "work_date": page["work_date"], "page_id": page["page_id"],
                "source_form": tpl.name, "equipment": equipment, "equipment_id": eid,
                "item": str(row_meta.get("item", r["row_key"])), "place": _opt(row_meta.get("place")),
                "column_name": r["field_name"], "shift": _opt(col_meta.get("shift")),
                "is_subtotal": int(bool(col_meta.get("subtotal") or row_meta.get("subtotal"))),
                "has_value_raw": int(bool(r["has_value_raw"])), "has_value": int(bool(r["has_value"])),
                "count": as_int(r["value_final"]), "count_raw": as_int(r["value_raw"]), "confidence": r["confidence"],
                "source_field_id": r["field_id"], "review_status": r["review_status"]})
    return usage, tally


def _formatted(cell) -> bool:
    """덩어리 배정으로 값 유무를 정하는 칸: 손으로 쓰는 칸 중 형식이 있는 것 (정수·소수·시각 …)."""
    return cell.kind.startswith("handwritten") and cell.fmt is not None


def _meter_slot(r: dict) -> str | None:
    return meter_slot(r["field_name"], r["row_key"])


def _state(r: dict):
    """최종 필드 행의 값: None(빈 칸) | UNKNOWN(잉크는 있는데 값이 없다 — 검수 대기·읽을 수 없음) | 정규화한 표기.
    검수 대기인데 값이 없으면 기계가 빈 칸이라 했어도 UNKNOWN 이다 — "읽을 수 없음" 검수는 기계의 값 유무를 그대로 두므로
    (has_value 0), 그것을 빈 칸으로 치면 가동 시간이 다음 근거로 내려간다 (추정)."""
    v = r["value_final"]
    if r["review_status"] == "pending" and v in (None, ""):
        return UNKNOWN
    if not r["has_value"]:
        return None
    if v in (None, ""):
        return UNKNOWN
    return try_normalize(r["format"], v)


def _meter_value(fmt: str | None, v: str) -> tuple[str, float | str] | None:
    """계기 칸의 정규화한 값 → ("meter", 수) | ("clock", "HH:MM"). 형식에 맞지 않으면(예전 검수 줄 등) None."""
    kind = "clock" if fmt == "time" else ("meter" if fmt == "decimal" else reading_kind(v))
    if kind == "meter" and as_number(v) is not None:
        return "meter", as_number(v)
    if kind == "clock" and minutes(v) is not None:
        return "clock", v
    return None


def _reading(vals: dict, meter: dict, has_meter: bool) -> tuple[str, dict]:
    """계기 칸의 종류와 값: (meter | clock | mixed | empty | pending | none, {"meter": {칸: 수}, "clock": {칸: "HH:MM"}}).
    vals 의 형식에 맞지 않는 값은 여기서 UNKNOWN 으로 바꾼다 (모르는 칸)."""
    if not has_meter:
        return "none", {}
    out: dict = {"meter": {}, "clock": {}}
    for slot, v in list(vals.items()):
        if v is None or v is UNKNOWN:
            continue
        mv = _meter_value(meter[slot]["format"], v)
        if mv is None:
            vals[slot] = UNKNOWN
        else:
            out[mv[0]][slot] = mv[1]
    if any(v is UNKNOWN for v in vals.values()):
        return "pending", out
    # 계기 값인지 시각인지는 시작·종료로 가른다. 총(가동시간)은 길이라 시각 옆에 숫자로 적혀도 섞인 것이 아니다
    kinds = [k for k in ("meter", "clock") if "start" in out[k] or "end" in out[k]]
    if len(kinds) > 1:
        return "mixed", out
    if kinds:
        return kinds[0], out
    return ("meter" if "total" in out["meter"] else ("clock" if "total" in out["clock"] else "empty")), out


def _shifts(rows: list[dict]) -> tuple[str | None, int | None]:
    """근무 시각: JSON {행 키: 범위} (값이 있는 칸만, 표가 없으면 NULL), 분의 합 (모르는 칸이 있거나 값이 하나도 없으면 NULL)."""
    if not rows:
        return None, None
    vals, total, unknown = {}, 0, False
    for r in sorted(rows, key=lambda x: (x["row_no"], x["x0"] or 0)):
        st = _state(r)
        if st is None:
            continue
        mins = None if st is UNKNOWN else range_minutes(st)
        if mins is None:
            unknown = True
            continue
        key = r["row_key"] if sum(x["row_key"] == r["row_key"] for x in rows) == 1 else f"{r['row_key']}/{r['field_name']}"
        vals[key] = st
        total += mins
    return json.dumps(vals, ensure_ascii=False, sort_keys=True), (None if unknown or not vals else total)


def _hours(vals: dict, kind: str, m: dict, shift_min: int | None, shift_rows: list[dict]) -> tuple[float | None, str | None]:
    """4.3 의 순서. 앞선 근거에 모르는 칸이 있으면 내려가지 않고 NULL. 계기 칸(시작·종료)이 계기 값과 시각으로 섞였으면 NULL.
    총은 수일 때만 근거가 된다 (시각으로 적힌 총은 쓰지 않는다)."""
    if kind == "mixed":
        return None, None
    meter, clock = m.get("meter", {}), m.get("clock", {})
    if vals.get("start") is UNKNOWN or vals.get("end") is UNKNOWN:
        return None, None
    if "start" in meter and "end" in meter:
        return round(meter["end"] - meter["start"], 4), "meter"
    if vals.get("total") is UNKNOWN:
        return None, None
    if "total" in meter:
        return round(meter["total"], 4), "total"
    if "start" in clock and "end" in clock:
        return round(span_minutes(clock["start"], clock["end"]) / 60, 4), "clock"
    if shift_rows and any(_state(r) is not None for r in shift_rows):
        return (None, None) if shift_min is None else (round(shift_min / 60, 4), "shifts")
    return None, None


def _activity_rows(tpl: Template, roles: dict, by_region: dict) -> int | None:
    """작업 표에서 글씨가 있는 줄의 수 (최종 값 유무). 소계 줄(행 메타 subtotal)은 세지 않는다. 작업 표가 없으면 NULL."""
    regs = [reg for reg, role in roles.items() if role == "activities"]
    if not regs:
        return None
    n = 0
    for reg in regs:
        skip = {r["row"] for r in tpl.region(reg)["rows"] if r.get("subtotal")}
        written = {r["row_no"] for r in by_region.get(reg, []) if r["kind"].startswith("handwritten") and r["has_value"]}
        n += len(written - skip)
    return n


def _signed(frows: list[dict]) -> int | None:
    sig = [r for r in frows if r["kind"] == "signature"]
    return None if not sig else int(any(r["has_value"] for r in sig))


def _raw(r: dict | None) -> str | None:
    return None if r is None or r["value_raw"] in (None, "") else r["value_raw"]


def _fid(r: dict | None) -> str | None:
    return None if r is None else r["field_id"]


def _opt(v) -> str | None:
    return None if v in (None, "") else str(v)
