"""자가 시험 (tasks/0009 4.6): `minedocscan selftest` — 설치한 프로그램만으로(pytest 없이), 합성 데이터만으로, 임시 폴더에서.

검사 (앞의 검사가 실패하면 그것에 기대는 뒤의 검사는 건너뛴다):
  synth        합성 묶음 이틀치(점검·운반·가동 일보 — 인쇄 층·표시 이름·가릴 상자)와 접수 폴더: 첫날은 날짜 있는 이름, 둘째 날은
               스캐너가 붙이는 이름 (파일명 규칙에 맞지 않는다)
  intake       접수 → 첫날은 처리하고 둘째 날은 날짜를 기다린다 → 날짜 결정 → 처리. 쪽이 다 적재되고 업무 행에 날짜가 있다
  recognition  정답 인식기(oracle): 기계가 읽는 표의 칸(형식 없음·integer — handlers.base.readable)의 CER 0, 읽지 않는 형식(소수·시각·
               계기)의 값이 있는 칸은 "잉크 있음 + 검수 대기" (tasks/0009 1절 라). 표 밖 필드는 세기만 한다 (check_recognition)
  reprocess    검수(읽지 않는 계기 칸에 정답, 운반 칸 하나)와 결정(쪽 버리기) → 다시 처리 = 같은 파일·검수·결정으로 처음부터 만든 DB
               (0007 의 불변식 — 받은 시각까지 같다)
  excel        일별·월별 파일을 써서 되읽기 = DB 의 모델, 운반 표 = 합성 정답, 정답을 검수로 넣은 계기 칸은 그 값
  masked       가린 쪽 그림: 상자 밖 = 정합 그림, 상자 안 = 한 색
  serve        `serve --port 0 --log-dir` 을 하위 프로세스로 띄워 /api/home·/export/day.xlsx 가 200, 로그 파일이 UTF-8
  publish      (--publish-schema) 통합 DB 의 minedocscan_selftest_* 스키마에 싣고 --check 가 같으면 그 스키마를 지운다 (--keep 이면 남긴다)
결과: OUT/selftest.json·selftest.md — 검사마다 통과·실패·건너뜀과 시간, 환경(OS·파이썬·의존성의 판·CPU 수·메모리). 사용자의 경로·이름을
넣지 않는다 (오류의 글에서도 임시 폴더·집 폴더·사용자 이름을 지운다). 망에 닿지 않는다 (--publish-schema 의 대상과 127.0.0.1 만).
"""
from __future__ import annotations

import getpass
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

SCHEMA_PREFIX = "minedocscan_selftest_"
SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
DAYS = 2
UNDATED = "SCAN_0001.pdf"            # 스캐너가 붙이는 이름 — 사이트 팩의 파일명 규칙(날짜)에 맞지 않는다
REVIEWER = "selftest"
SERVE_WAIT_S = 90.0                  # serve 가 포트를 알리기까지 (러너에서 2–5초 — 처음 import 가 느린 PC 를 넉넉히)
HTTP_TIMEOUT_S = 60.0                # /export/day.xlsx 는 그때 만든다 (합성 하루치 1초 안)


class CheckFailed(Exception):
    """검사가 기대와 다르다 — 글은 한 줄, 경로·이름 없이."""


class CheckSkipped(Exception):
    """검사를 할 수 없다 (그 검사만 건너뛴다 — 예: 통합 DB 의 CREATE 권한이 없다)."""


@dataclass
class Ctx:
    root: Path
    publish_schema: str | None = None
    keep: bool = False
    synth: object = None
    st: object = None
    site: object = None
    answers: dict = field(default_factory=dict)
    days: list[str] = field(default_factory=list)
    pipe: object = None
    worker: object = None
    undated: str | None = None
    reviewed_page: str | None = None          # 계기 칸을 검수한 쪽의 출처 ("<이름>#<쪽>")
    reviewed_meter: dict = field(default_factory=dict)   # 그 쪽의 업무 시트 열 → 검수로 넣은 값
    discarded: str | None = None              # 버린 쪽 ("<문서 ID>-p<쪽>")
    dropped_log: tuple | None = None          # 버린 운반 일보 쪽의 (날짜, 자리) — 운반 표에서 그 자리는 "일보 없음"
    closers: list = field(default_factory=list)


@dataclass
class Check:
    name: str
    title: str
    needs: tuple[str, ...]
    fn: Callable[[Ctx], dict]


# ── 합성 묶음과 접수 폴더 ───────────────────────────────────────────────────
def make_synth(out: Path):
    """합성 묶음 이틀치 (시험이 바꿔 끼운다 — 같은 묶음을 복사해 시간을 아낀다)."""
    from .tools.synth import generate

    return generate(out, days=DAYS, seed=0, usage_logs=True, print_layers=True, display_names=True)


def check_synth(ctx: Ctx) -> dict:
    from .config import Settings
    from .recognize import load_answers_json

    syn = make_synth(ctx.root / "합성")
    pdfs = sorted(Path(syn.scans).glob("*.pdf"))
    if len(pdfs) != DAYS:
        raise CheckFailed(f"합성 묶음의 파일이 {len(pdfs)}개다 ({DAYS}개여야 한다)")
    inbox, archive = ctx.root / "스캐너", ctx.root / "보관"
    inbox.mkdir()
    archive.mkdir()
    shutil.copyfile(pdfs[0], inbox / pdfs[0].name)                    # 날짜 있는 이름
    shutil.copyfile(pdfs[1], inbox / UNDATED)                          # 스캐너가 붙인 이름 — 사람이 날짜를 넣는다
    rename = {pdfs[1].stem: Path(UNDATED).stem}
    labels = Path(syn.site) / "labels" / "pages.json"                 # 합성의 쪽 라벨(차량번호·작성자)은 파일 이름으로 찾는다 — 따라간다
    if labels.is_file():                                               # (synth --intake 가 이름을 바꾼 파일에 하는 것과 같다)
        lab = json.loads(labels.read_text(encoding="utf-8"))
        for k in list(lab):
            stem, sep, page = k.partition("#")
            if stem in rename:
                lab[rename[stem] + sep + page] = dict(lab[k])
        labels.write_text(json.dumps(lab, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    answers = {}
    for (origin, *rest), v in load_answers_json(syn.answers_path).items():
        stem, sep, page = origin.partition("#")
        answers[(rename.get(stem, stem) + sep + page, *rest)] = v
    ctx.synth, ctx.answers = syn, answers
    ctx.days = sorted(d["date"] for d in syn.truth["days"])
    ctx.st = Settings(site=Path(syn.site), archive_root=archive, work_root=ctx.root / "작업",
                      reviews=ctx.root / "기록" / "reviews.jsonl", inbox=inbox, settle_seconds=0.0, give_up_seconds=0.0)
    return {"days": len(ctx.days), "files": len(pdfs), "answers": len(answers)}


# ── 접수와 처리 ─────────────────────────────────────────────────────────────
def _oracle(ctx: Ctx):
    from .recognize import OracleRecognizer

    return OracleRecognizer(ctx.answers)


def check_intake(ctx: Ctx) -> dict:
    from .forms.sitepack import SitePack
    from .intake import decisions as decs
    from .intake.inbox import Inbox
    from .intake.worker import Worker
    from .pipeline import Pipeline

    st = ctx.st
    ctx.site = SitePack(st.site)
    ctx.pipe = pipe = Pipeline(st, site=ctx.site, recognizer=_oracle(ctx))
    ctx.closers.append(pipe.con)
    ctx.worker = w = Worker(pipe, Inbox(st, pipe.con, settle_seconds=0.0, give_up_seconds=0.0))
    first = w.run_once()
    if len(first.get("received") or []) != DAYS:
        raise CheckFailed(f"접수 폴더에서 받은 문서가 {len(first.get('received') or [])}개다 ({DAYS}개여야 한다)")
    waiting = first.get("needs_date") or []
    if len(waiting) != 1:
        raise CheckFailed(f"날짜를 기다리는 문서가 {len(waiting)}개다 (스캐너 이름의 문서 1개여야 한다)")
    ctx.undated = waiting[0]
    decs.save(pipe.con, st.decisions_path(ctx.site.root), [{"target": ctx.undated, "kind": "date", "value": ctx.days[1]}],
              REVIEWER)
    second = w.run_once()
    if second.get("processed", 0) < 1:
        raise CheckFailed("날짜를 넣은 문서를 처리하지 않았다")
    con = pipe.con
    status = Counter(r[0] for r in con.execute("SELECT status FROM doc_page"))
    total = sum(r[0] or 0 for r in con.execute("SELECT n_pages FROM doc_document"))
    if not total or status.get("loaded", 0) != total:
        raise CheckFailed(f"쪽 {total}장 중 적재된 쪽 {status.get('loaded', 0)}장 (상태 {dict(status)})")
    dates = sorted(r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page"))
    if dates != ctx.days:
        raise CheckFailed(f"쪽의 날짜가 합성의 날짜와 다르다 (날짜 {len(dates)}개)")
    from .store.db import PUBLISH_TABLES
    for t in PUBLISH_TABLES:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        for c in ("work_date", "inspection_date"):
            if c in cols and t != "doc_document":
                n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} IS NULL").fetchone()[0]
                if n:
                    raise CheckFailed(f"{t} 에 날짜 없는 행 {n}개")
    return {"documents": DAYS, "pages": total, "needs_date_then_dated": 1}


# ── 인식 (정답 인식기) ──────────────────────────────────────────────────────
def _field_rows(con, where: str = "", args: tuple = ()) -> list:
    return con.execute(
        "SELECT f.field_id, f.page_id, d.source_name, p.page_no, p.work_date, p.template_name, f.region, f.field_name, f.row_key, "
        "f.format, f.kind, f.value_raw, f.has_value_raw, f.status_raw, f.review_status, p.document_id "
        "FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
        f"WHERE f.kind LIKE 'handwritten%' AND p.status = 'loaded' {where}", args).fetchall()


def _truth_of(ctx: Ctx):
    """doc_field 행 → 정답 글자 (그 표에 정답이 없으면 None). 오라클과 같은 순서: 쪽 출처, 없으면 날짜."""
    key = ctx.site.answer_key
    answers = {(o, key(t), *rest): v for (o, t, *rest), v in ctx.answers.items()}
    tables = {(o, t, r) for (o, t, r, *_rest) in answers}

    def truth(r) -> str | None:
        t, src = key(r["template_name"]), f"{r['source_name']}#{r['page_no']}"
        origin = src if (src, t, r["region"]) in tables else r["work_date"]
        if (origin, t, r["region"]) not in tables:
            return None
        return answers.get((origin, t, r["region"], r["field_name"], r["row_key"] or ""), "")
    return truth


PAGE_FIELD_MISSES = 0.1          # 표 밖 필드 중 값이 있는데 빈 칸으로 본 것의 상한 (비율) — 아래


def check_recognition(ctx: Ctx) -> dict:
    from .evaluate.fields import evaluate_fields
    from .evaluate.metrics import corpus_cer, normalize
    from .forms.formats import try_normalize
    from .handlers.base import readable

    truth_of = _truth_of(ctx)
    pairs, unread, bad_unread = [], 0, 0
    page_fields = Counter()
    for r in _field_rows(ctx.pipe.con):
        truth = truth_of(r)
        if truth is None:
            continue
        fmt = r["format"]
        machine = (r["value_raw"] or "") if r["has_value_raw"] else ""
        if r["region"] == "fields" and normalize(truth) and not normalize(machine):
            # 표 밖 필드의 값 유무는 큰 상자 전체의 잉크 비율이라 합성 메모(낱말 둘)의 일부가 문턱(약 0.008) 아래로 빈 칸이 된다
            # (이틀치 36개 중 2개, 잉크 0.0076). 파이프라인의 판정이지 인식의 오류가 아니다 — 세되 상한을 둔다 (값을 읽은 것은 아래에서 CER)
            page_fields["seen_empty"] += 1
            continue
        if r["region"] == "fields":
            page_fields["read"] += 1
        if readable(SimpleNamespace(fmt=fmt)):
            pairs.append((try_normalize(fmt, machine) or "", try_normalize(fmt, truth) or ""))
        elif normalize(truth):
            unread += 1
            bad_unread += not (r["has_value_raw"] == 1 and r["status_raw"] == "pending")
    if not pairs:
        raise CheckFailed("기계가 읽는 칸이 없다")
    missing = evaluate_fields(ctx.pipe.con, ctx.answers, target="raw", site=ctx.site)["answers_not_in_db"]
    if missing:                                                        # 쪽은 적재됐는데 칸이 빠졌다 — 위의 비교는 있는 칸만 본다
        raise CheckFailed(f"정답이 있는 칸 {missing}개에 doc_field 행이 없다")
    have = Counter(r["page_id"] for r in _field_rows(ctx.pipe.con))   # 빈 칸·표 하나가 통째로 빠진 것도 (정답은 값 있는 칸만 안다)
    short = 0
    for pid, tname in ctx.pipe.con.execute("SELECT page_id, template_name FROM doc_page WHERE status = 'loaded'"):
        tpl = ctx.site.templates[tname]
        short += sum(c.kind.startswith("handwritten") for c in tpl.cells() + tpl.field_cells()) != have[pid]
    if short:
        raise CheckFailed(f"쪽 {short}장의 손글씨 칸 수가 템플릿과 다르다")
    cer = corpus_cer(pairs)
    wrong = sum(1 for a, b in pairs if normalize(a) != normalize(b))
    if cer != 0:
        raise CheckFailed(f"기계가 읽는 칸 {len(pairs)}개의 CER {cer:.4f} (틀린 칸 {wrong}개) — 정답 인식기로는 0 이어야 한다 "
                          "(인식기가 아니라 파이프라인의 문제)")
    if not unread:
        raise CheckFailed("값이 있는 읽지 않는 칸(소수·시각·계기)이 없다")
    if bad_unread:
        raise CheckFailed(f"값이 있는 읽지 않는 칸 {unread}개 중 {bad_unread}개가 '잉크 있음 + 검수 대기'가 아니다")
    valued = page_fields["read"] + page_fields["seen_empty"]
    if page_fields["seen_empty"] > max(2, PAGE_FIELD_MISSES * valued):
        raise CheckFailed(f"값이 있는 표 밖 필드 {valued}개 중 {page_fields['seen_empty']}개를 빈 칸으로 봤다 (상한 {PAGE_FIELD_MISSES:.0%})")
    return {"read_cells": len(pairs), "cer": 0.0, "unread_cells_with_value": unread,
            "page_fields_read": page_fields["read"], "page_fields_seen_empty": page_fields["seen_empty"]}


# ── 검수·결정 → 다시 처리 = 처음부터 ───────────────────────────────────────
METER_COLUMNS = {"start": "계기 시작", "end": "계기 종료", "total": "계기 총"}   # 계기 표(role meter)의 칸 → 가동 기록 시트의 열


def _dump(con, skip: tuple[str, ...]) -> dict:
    """테이블마다 (열, 정렬한 행) (meta_schema·eq_equipment 빼고 — 장비 마스터는 템플릿에서 온다)."""
    out = {}
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
        if t in ("meta_schema", "eq_equipment") or t.startswith("sqlite_"):
            continue
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})") if r[1] not in skip]
        out[t] = (cols, sorted((tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM {t}")), key=repr))
    return out


def _differences(a: dict, b: dict) -> list[str]:
    """두 덤프가 다른 표와 열 (값 없이 — 표 이름·열 이름·행 수만)."""
    out = []
    for t in sorted(set(a) | set(b)):
        if a.get(t) == b.get(t):
            continue
        (ca, ra), (cb, rb) = a.get(t, ([], [])), b.get(t, ([], []))
        cols = [c for i, c in enumerate(ca) if ca == cb and sorted(map(repr, (r[i] for r in ra))) != sorted(map(repr, (r[i] for r in rb)))]
        out.append(f"{t} ({len(ra)}행 대 {len(rb)}행{', 열 ' + '·'.join(cols[:4]) if cols else ''})")
    return out


def check_reprocess(ctx: Ctx) -> dict:
    from .forms.formats import try_normalize
    from .forms.sitepack import SitePack
    from .handlers.base import readable
    from .intake import decisions as decs
    from .pipeline import Pipeline
    from .review.store import review_from_field, save
    from .store.db import PUBLISH_SKIP_COLUMNS

    con, site, st = ctx.pipe.con, ctx.site, ctx.st
    truth_of = _truth_of(ctx)
    # 1) 읽지 않는 계기 칸에 정답 — 계기 칸에 값이 둘 이상인 첫날의 가동 일보 쪽 하나 (그 쪽의 계기 칸 전부: 값 또는 빈 칸)
    meter: dict[str, list] = {}
    for r in _field_rows(con, "AND p.work_date = ? AND f.region = 'meter'", (ctx.days[0],)):
        if not readable(SimpleNamespace(fmt=r["format"])) and r["field_name"] in METER_COLUMNS and truth_of(r) is not None:
            meter.setdefault(r["page_id"], []).append(r)
    page = next((p for p, rows in sorted(meter.items()) if sum(1 for r in rows if truth_of(r)) >= 2), None)
    if page is None:
        raise CheckFailed("계기 칸에 값이 둘 이상인 가동 일보 쪽이 없다")
    reviews = []
    for r in meter[page]:
        v = try_normalize(r["format"], truth_of(r) or "") or ""
        reviews.append(review_from_field(con, r["field_id"], "value" if v else "empty", v, REVIEWER))
        if v:
            ctx.reviewed_meter[METER_COLUMNS[r["field_name"]]] = v
        ctx.reviewed_page = f"{r['source_name']}#{r['page_no']}"
    # 2) 운반 일보 칸 하나에 정답 (기계 값과 같아도 — 검수가 이긴다)
    haul = con.execute("SELECT h.source_field_id FROM prod_haul h WHERE h.source_role = 'log' AND h.source_field_id IS NOT NULL "
                       "ORDER BY h.work_date, h.source_field_id LIMIT 1").fetchone()
    if haul is None:
        raise CheckFailed("운반 일보 행이 없다")
    hrow = _field_rows(con, "AND f.field_id = ?", (haul[0],))[0]
    hv = truth_of(hrow) or ""
    reviews.append(review_from_field(con, haul[0], "value" if hv else "empty", hv, REVIEWER))
    for rv in reviews:
        save(con, site, st, rv)
    # 3) 결정: 둘째 날 문서의 마지막 쪽(가동 일보)과 첫 운반 일보 쪽을 버린다 (그 자리는 운반 표에서 "일보 없음"이 된다)
    n = con.execute("SELECT n_pages FROM doc_document WHERE document_id = ?", (ctx.undated,)).fetchone()[0]
    ctx.discarded = f"{ctx.undated}-p{n}"
    pages = ctx.synth.truth["documents"][sorted(ctx.synth.truth["documents"])[1]]
    log = next(pg for pg in pages if pg.get("slot"))
    ctx.dropped_log = (ctx.days[1], log["slot"])
    decs.save(con, st.decisions_path(site.root), [{"target": ctx.discarded, "kind": "discard"},
                                                  {"target": f"{ctx.undated}-p{log['page']}", "kind": "discard"}], REVIEWER)
    r = ctx.worker.run_once()
    if r.get("processed", 0) < 1:
        raise CheckFailed("결정을 넣은 문서를 다시 처리하지 않았다")
    gone = con.execute("SELECT status FROM doc_page WHERE document_id = ? AND page_no = ?", (ctx.undated, n)).fetchone()
    if gone is None or gone[0] != "discarded":
        raise CheckFailed("버린 쪽의 상태가 discarded 가 아니다")
    # 처음부터: 같은 보관 폴더·검수 파일·결정 파일로 새 작업 폴더에
    fresh = Pipeline(replace(st, work_root=ctx.root / "처음부터", inbox=None), site=SitePack(site.root),
                     recognizer=_oracle(ctx))
    ctx.closers.append(fresh.con)
    fresh.run([st.archive_root])
    skip = tuple(c for c in PUBLISH_SKIP_COLUMNS if c != "received_at")   # 받은 시각도 같아야 한다 (보관 폴더의 이름에서)
    a, b = _dump(con, skip), _dump(fresh.con, skip)
    diff = _differences(a, b)
    fresh.con.close()
    if diff:
        raise CheckFailed(f"다시 처리한 DB 가 처음부터 만든 DB 와 다르다: {', '.join(diff[:5])}")
    return {"reviews": len(reviews), "decisions": 3, "tables_compared": len(a),
            "rows_compared": sum(len(rows) for _cols, rows in a.values())}


# ── 엑셀 ────────────────────────────────────────────────────────────────────
def _model_rows(sheet: dict) -> list[list]:
    """모델의 시트 → 되읽은 것과 같은 모양 (만든 시각·판 칸은 비운다, 끝의 빈 칸·빈 줄은 뺀다)."""
    rows = []
    for row in sheet["rows"]:
        vals = [None if c[1].startswith("stamp") else c[0] for c in row]
        while vals and vals[-1] is None:
            vals.pop()
        rows.append(vals)
    while rows and not rows[-1]:
        rows.pop()
    return rows


def _same_as_model(got: dict, book: dict, rel: str) -> None:
    if list(got) != [s["name"] for s in book["sheets"]]:
        raise CheckFailed(f"{rel}: 시트가 모델과 다르다")
    for s in book["sheets"]:
        want, have = _model_rows(s), list(got[s["name"]])
        for i, row in enumerate(s["rows"]):
            if any(c[1].startswith("stamp") for c in row) and i < len(have):
                if not have[i][1:2] or not have[i][1]:
                    raise CheckFailed(f"{rel} 의 {s['name']}: 만든 시각이 비었다")
                have[i], want[i] = have[i][:1], want[i][:1]
        while have and not have[-1]:
            have.pop()
        if have != want:
            raise CheckFailed(f"{rel} 의 시트 {s['name']} 가 모델과 다르다")


def _haul_table_values(rows: list[list], keys: dict[str, str]) -> tuple[dict, Counter]:
    """되읽은 운반 표 → ({(날짜, 자리, "광종|편", 주야 키): 횟수}, {(날짜, 자리): 문서 없음 칸의 수}). keys: 표시 이름 → 키."""
    from .export import labels as L

    shift = {v: k for k, v in L.SHIFT.items()}
    top, names = rows[0], rows[1]
    starts = [i for i, v in enumerate(top) if v] + [len(names)]
    values, no_doc = {}, Counter()
    for row in rows[2:]:
        row = list(row) + [None] * (len(names) - len(row))
        day, sh = row[0], shift.get(row[1], row[1])
        for a, z in zip(starts, starts[1:], strict=False):
            title = top[a]
            for j in range(a, z):
                col = names[j]
                if col in (L.SOURCE, L.HAUL_TABLE_SUM):
                    continue
                v = row[j]
                if v == L.NO_DOC_MARK:
                    no_doc[(day, title)] += 1
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    values[(day, title, keys.get(col, col), sh)] = int(v)
    return values, no_doc


def check_excel(ctx: Ctx) -> dict:
    from .export import labels as L
    from .export.daily import daily_book
    from .export.monthly import haul_columns, monthly_book
    from .export.writer import DAILY_RE, MONTHLY_RE, daily_path, export_excel, monthly_path
    from .export.xlsx import read_values
    from .store.db import read_txn

    con, site = ctx.pipe.con, ctx.site
    out = ctx.root / "엑셀"
    out.mkdir()
    res = export_excel(con, site, out, full=True)
    if res.failed:
        raise CheckFailed(f"쓰지 못한 파일 {len(res.failed)}개")
    days = sorted(r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date IS NOT NULL"))
    want = sorted([daily_path(d) for d in days] + [monthly_path(m) for m in {d[:7] for d in days}])
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.xlsx"))
    if files != want:
        raise CheckFailed(f"파일 {len(files)}개 (일별·월별 {len(want)}개여야 한다)")
    got_all = {}
    for rel in files:
        with read_txn(con):
            if m := DAILY_RE.match(rel):
                book = daily_book(con, site, m.group(2))
            else:
                m = MONTHLY_RE.match(rel)
                book = monthly_book(con, site, m.group(1), [d for d in days if d.startswith(m.group(1))])
        got_all[rel] = got = read_values(out / rel)
        _same_as_model(got, book, rel)
    # 운반 표 = 합성 정답
    truth = ctx.synth.truth
    want_haul = {(d["date"], h["slot"], f"{h['material']}|{h['level']}", h["shift"]): h["trips"]
                 for d in truth["days"] for h in d["haul_log"]}
    has_log = {(d["date"], t["slot"]) for d in truth["days"] for t in d["trucks"] if t["has_log"]} - {ctx.dropped_log}
    no_log = {(d["date"], t["slot"]) for d in truth["days"] for t in d["trucks"] if not t["has_log"]} | {ctx.dropped_log}
    want_haul = {k: v for k, v in want_haul.items() if (k[0], k[1]) in has_log}           # 버린 일보의 횟수는 표에 없다
    keys = {disp: key for key, disp in haul_columns(site, set())}
    values, no_doc = {}, Counter()
    for m in sorted({d[:7] for d in days}):
        rows = got_all[monthly_path(m)].get(L.SHEETS["haul_table"])
        if not rows:
            raise CheckFailed(f"월별 파일 {m} 에 운반 표가 없다")
        v, nd = _haul_table_values(rows, keys)
        values.update(v)
        no_doc.update(nd)
    got_haul = {k: v for k, v in values.items() if (k[0], k[1]) in has_log}
    if got_haul != want_haul:
        bad = len(set(got_haul.items()) ^ set(want_haul.items()))
        raise CheckFailed(f"운반 표가 합성 정답과 다르다 (어긋난 칸 {bad}개, 정답 칸 {len(want_haul)}개)")
    if set(no_doc) != no_log:
        raise CheckFailed("운반 표의 '일보 없음' 자리가 합성 정답과 다르다")
    # 정답을 검수로 넣은 계기 칸은 그 값 (일별 파일의 가동 기록 시트)
    sheet = got_all[daily_path(ctx.days[0])].get(L.SHEETS["usage"]) or []
    head = sheet[0] if sheet else []
    row = next((r for r in sheet[1:] if L.SOURCE in head and len(r) > head.index(L.SOURCE)
                and r[head.index(L.SOURCE)] == ctx.reviewed_page), None)
    if row is None:
        raise CheckFailed("검수한 쪽이 가동 기록 시트에 없다")
    for col, v in ctx.reviewed_meter.items():
        cell = row[head.index(col)] if head.index(col) < len(row) else None
        if cell is None or float(cell) != float(v):
            raise CheckFailed(f"검수로 넣은 '{col}' 이 엑셀에 그 값으로 나오지 않는다")
    return {"files": len(files), "haul_cells": len(want_haul), "no_log_slots": len(no_log),
            "reviewed_cells_shown": len(ctx.reviewed_meter)}


# ── 가린 그림 ───────────────────────────────────────────────────────────────
def check_masked(ctx: Ctx) -> dict:
    import numpy as np

    from .export.masked import FILL, KINDS, export_masked, page_boxes, redact_settings
    from .imaging.io import imread_gray
    from .tools.printlayer import page_image

    con, site, st = ctx.pipe.con, ctx.site, ctx.st
    out = ctx.root / "가린 그림"
    day = ctx.days[0]
    res = export_masked(con, site, st, out, date=day)
    if not all(res["boxes"].get(k) for k in KINDS):                   # 합성 템플릿(표시 이름·가릴 상자)에는 네 종류가 다 있다
        raise CheckFailed(f"가린 상자의 종류가 빠졌다 ({', '.join(k for k in KINDS if not res['boxes'].get(k))})")
    rows = con.execute("SELECT p.page_id, p.document_id, p.page_no, p.status, p.work_date, p.template_name, p.aligned_image, "
                       "p.homography, p.render_dpi, d.source_path, d.source_rel FROM doc_page p "
                       "JOIN doc_document d ON p.document_id = d.document_id WHERE p.work_date = ? AND p.status = 'loaded' "
                       "ORDER BY p.page_id", (day,)).fetchall()
    if res["pages"] != len(rows) or not rows:
        raise CheckFailed(f"가린 쪽 {res['pages']}장 (적재된 쪽 {len(rows)}장이어야 한다)")
    meta_keys, pad = redact_settings(site)
    masked_px = 0
    for r in rows:
        tpl = site.templates[r["template_name"]]
        img = imread_gray(out / f"{r['page_id']}.png")
        ref, _how = page_image(r, tpl, st, tpl.reference.shape)
        if ref is None or img is None or img.shape != ref.shape:
            raise CheckFailed("가린 그림과 정합 그림의 크기가 다르다")
        inside = np.zeros(ref.shape[:2], bool)
        h, w = inside.shape
        for _kind, (x0, y0, x1, y1) in page_boxes(tpl, meta_keys, pad):
            inside[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
        if not np.array_equal(img[~inside], ref[~inside]):
            raise CheckFailed("가린 그림의 상자 밖이 정합 그림과 다르다")
        if not (img[inside] == FILL).all():
            raise CheckFailed("가린 그림의 상자 안이 한 색이 아니다")
        masked_px += int(inside.sum())
    if not masked_px:
        raise CheckFailed("가린 자리가 없다 (합성 템플릿에는 서명·작성자·가릴 상자가 있다)")
    return {"pages": len(rows), "boxes": sum(res["boxes"].values()), "pad_px": pad}


# ── 화면 ────────────────────────────────────────────────────────────────────
def _toml_str(p: Path) -> str:
    return json.dumps(str(p))                                          # TOML 의 기본 글자열과 같은 escape (\\, \uXXXX)


def serve_command(config: Path, logs: Path) -> list[str]:
    """띄울 명령 (시험이 바꿔 끼운다). 작업 스케줄러가 띄우는 것과 같은 모양 — 포트만 빈 것으로."""
    return [sys.executable, "-m", "minedocscan.cli", "serve", "--config", str(config), "--reviewer", REVIEWER, "--port", "0",
            "--log-dir", str(logs)]


def _get(url: str) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1 — 프록시를 거치지 않는다
    try:
        with opener.open(url, timeout=HTTP_TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def check_serve(ctx: Ctx) -> dict:
    st = ctx.st
    cfg = ctx.root / "minedocscan.toml"
    cfg.write_text("[paths]\n" + "".join(f"{k} = {_toml_str(v)}\n" for k, v in (
        ("site", st.site), ("archive_root", st.archive_root), ("work_root", st.work_root), ("reviews", st.reviews_path()),
        ("inbox", st.inbox))) + "\n[intake]\nsettle_seconds = 0\ngive_up_seconds = 0\n", encoding="utf-8")
    logs = ctx.root / "logs"
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINEDOCSCAN_")}   # 이 PC 의 설정·통합 DB 를 쓰지 않는다
    err = ctx.root / "serve-stderr.txt"
    with open(err, "wb") as ef:
        proc = subprocess.Popen(serve_command(cfg, logs), env=env, cwd=str(ctx.root), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=ef)
    try:
        port, deadline, text = None, time.monotonic() + SERVE_WAIT_S, ""
        while port is None and time.monotonic() < deadline:
            for p in sorted(logs.glob("serve-*.log")) if logs.is_dir() else []:
                text = p.read_bytes().decode("utf-8", errors="replace")
                m = re.search(r"http://127\.0\.0\.1:(\d+)/", text)
                if m:
                    port = int(m.group(1))
            if port is None:
                if proc.poll() is not None:
                    last = (text.strip().splitlines() or err.read_text("utf-8", errors="replace").strip().splitlines() or ["?"])[-1]
                    raise CheckFailed(f"serve 가 포트를 알리기 전에 끝났다 (종료 코드 {proc.returncode}): {last}")
                time.sleep(0.2)
        if port is None:
            raise CheckFailed(f"serve 가 {SERVE_WAIT_S:g}초 안에 포트를 알리지 않았다")
        code, body = _get(f"http://127.0.0.1:{port}/api/home")
        if code != 200:
            raise CheckFailed(f"/api/home 이 {code}")
        json.loads(body.decode("utf-8"))
        code, body = _get(f"http://127.0.0.1:{port}/export/day.xlsx?date={ctx.days[0]}")
        if code != 200 or not body.startswith(b"PK"):
            raise CheckFailed(f"/export/day.xlsx 가 {code}")
        files = sorted(logs.glob("serve-*.log"))
        try:
            log = files[-1].read_bytes().decode("utf-8")
        except (IndexError, UnicodeDecodeError):
            raise CheckFailed("로그 파일이 없거나 UTF-8 이 아니다") from None
        if not any("가" <= ch <= "힣" for ch in log):
            raise CheckFailed("로그 파일에 한글이 없다 (글자가 바뀌었다)")
        return {"api_home": 200, "export_day_xlsx": 200, "log_files": len(files), "xlsx_bytes": len(body)}
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(15)


# ── 통합 DB ────────────────────────────────────────────────────────────────
def check_publish(ctx: Ctx) -> dict:
    if not ctx.publish_schema:
        raise CheckSkipped("요청하지 않았다 (--publish-schema)")
    from .publish import core
    from .store.db import PUBLISH_TABLES

    url = (os.environ.get("MINEDOCSCAN_PUBLISH_URL") or "").strip()
    if not url:
        raise CheckFailed("--publish-schema 에는 환경변수 MINEDOCSCAN_PUBLISH_URL 이 있어야 한다")
    s = replace(ctx.st, publish_url=url, publish_schema=ctx.publish_schema)
    name = core.site_name(s)
    schema = ctx.publish_schema

    def schema_exists() -> bool:
        t = core.open_target(s)
        try:
            with t.cur() as c:
                c.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
                return c.fetchone() is not None
        finally:
            t.rollback()
            t.close()

    made = False
    try:
        if schema_exists():
            raise CheckFailed("그 스키마가 이미 있다 — 자가 시험이 만들지 않은 것은 쓰지도 지우지도 않는다 (다른 이름으로)")
        try:
            res = core.run(ctx.pipe.con, s, full=True, site=name)
        except core.PublishError as e:
            if e.kind == "InsufficientPrivilege":
                raise CheckSkipped("통합 DB 에 스키마를 만들 권한(CREATE)이 없다 — 이 검사만 건너뛴다") from None
            raise
        made = True
        chk = core.run(ctx.pipe.con, s, full=True, check=True, site=name)
        if chk.changed:
            raise CheckFailed(f"실은 뒤 --check 가 다르다고 한다 (다른 범위 {chk.changed})")
        t = core.open_target(s)                                        # --check 는 지문을 본다 — 행도 센다 (표마다 대상 = 작업 DB)
        try:
            with t.cur() as c:
                for table in PUBLISH_TABLES:
                    c.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
                    there = c.fetchone()[0]
                    here = ctx.pipe.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if there != here:
                        raise CheckFailed(f"대상의 {table} 이 {there}행 (작업 DB {here}행)")
        finally:
            t.rollback()
            t.close()
        out = {"rows": res.rows, "check": 0, "kept": ctx.keep}
        if not ctx.keep:
            t = core.open_target(s)
            try:
                t.drop()
                with t.cur() as c:
                    c.execute(f'DROP SCHEMA "{schema}"')
                t.commit()
            finally:
                t.close()
            if schema_exists():
                raise CheckFailed("다 실은 뒤 스키마를 지우지 못했다")
        return out
    except CheckSkipped:
        raise
    except (CheckFailed, core.PublishError) as e:
        why = str(e) if isinstance(e, CheckFailed) else f"통합 DB: {core.scrub(e.for_command(), url)} (종류 {e.kind})"
        left = " — 자가 시험이 만든 스키마를 살펴보도록 남겼다 (다시 돌리려면 지우거나 다른 이름으로)" if made else ""
        raise CheckFailed(why + left) from None
    except Exception as e:                                             # noqa: BLE001 — 자가 시험이 보낸 SQL: 예외의 종류만 (드라이버의 글 없이)
        f = core._failure(e, s)
        left = " — 자가 시험이 만든 스키마를 남겼다" if made else ""
        raise CheckFailed(f"통합 DB: {core.scrub(f.for_command(), url)} (종류 {f.kind}){left}") from None


CHECKS: list[Check] = [
    Check("synth", "합성 묶음 이틀치와 접수 폴더", (), check_synth),
    Check("intake", "접수 → 날짜 결정 → 처리, 쪽이 다 적재", ("synth",), check_intake),
    Check("recognition", "기계가 읽는 표의 칸의 CER 0, 읽지 않는 칸은 잉크 있음 + 검수 대기", ("intake",), check_recognition),
    Check("reprocess", "검수·결정 → 다시 처리 = 처음부터 만든 DB", ("intake",), check_reprocess),
    Check("excel", "일별·월별 되읽기 = 모델, 운반 표 = 정답, 검수한 칸은 그 값", ("reprocess",), check_excel),
    Check("masked", "가린 그림: 상자 밖 = 정합 그림, 안 = 한 색", ("intake",), check_masked),
    Check("serve", "화면: /api/home·/export/day.xlsx 가 200, 로그 파일 UTF-8", ("intake",), check_serve),
    Check("publish", "통합 DB 에 싣고 --check, 스키마 지우기", ("intake",), check_publish),
]


# ── 환경과 결과 ─────────────────────────────────────────────────────────────
def _memory_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = MemoryStatus()
            m.dwLength = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return None
            return round(m.ullTotalPhys / 2**30, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (AttributeError, OSError, ValueError):
        return None


DEPENDENCIES = ("numpy", "opencv-python-headless", "opencv-python", "pypdfium2", "openpyxl", "PyYAML", "psycopg", "psycopg-binary")


def environment() -> dict:
    """OS·파이썬·의존성의 판·CPU 수·메모리 — 호스트 이름·사용자·경로 없이."""
    from importlib import metadata

    from . import __version__

    deps = {}
    for d in DEPENDENCIES:
        try:
            deps[d] = metadata.version(d)
        except metadata.PackageNotFoundError:
            pass
    try:
        import cv2

        deps["cv2"] = cv2.__version__
    except ImportError:
        pass
    return {"minedocscan": __version__, "os": f"{platform.system()} {platform.release()}", "os_version": platform.version(),
            "machine": platform.machine(), "python": platform.python_version(),
            "python_implementation": platform.python_implementation(), "dependencies": deps, "cpu_count": os.cpu_count(),
            "memory_gb": _memory_gb()}


def _scrubber(root: Path) -> Callable[[str], str]:
    """글에서 임시 폴더·집 폴더·사용자 이름을 지운다 (결과 파일에 넣기 전에)."""
    subs = []
    for p, name in ((root, "<임시 폴더>"), (Path(tempfile.gettempdir()), "<임시>"), (Path.home(), "<집>")):
        for v in {str(p), str(p.resolve()) if p.exists() else str(p), p.as_posix()}:
            # OSError 의 글은 파일 이름을 repr 로 싣는다 — 윈도우에서는 역슬래시가 두 겹이다 (C:\\Users\\…)
            for form in {v, v.replace("\\", "\\\\")}:
                if form and len(form) > 3:
                    subs.append((form, name))
    try:
        user = getpass.getuser()
    except Exception:                                                  # noqa: BLE001 — 사용자 이름을 모르면 지울 것도 없다
        user = ""
    subs.sort(key=lambda x: -len(x[0]))

    def scrub(text: str) -> str:
        for v, name in subs:
            text = re.sub(re.escape(v), name, text, flags=re.IGNORECASE) if sys.platform == "win32" else text.replace(v, name)
        if user and len(user) >= 3:
            text = re.sub(re.escape(user), "<사용자>", text, flags=re.IGNORECASE)
        return text
    return scrub


REASON_CHARS = 400


def _line(text: str, tail: str = "") -> str:
    """한 줄로, 길면 자른다 — 경로를 지운 뒤에 (자르다 경로의 가운데가 남지 않게)."""
    text = " ".join(text.split())
    return (text if len(text) <= REASON_CHARS else text[:REASON_CHARS] + "…") + tail


@dataclass
class Outcome:
    name: str
    title: str
    status: str                     # passed | failed | skipped
    seconds: float
    detail: dict = field(default_factory=dict)
    reason: str = ""


def run(out: Path, keep: bool = False, publish_schema: str | None = None, checks: list[Check] | None = None,
        echo: Callable[[str], None] | None = None) -> dict:
    """검사를 차례로. 돌려주는 값: 결과 (selftest.json 과 같은 것). out 에 selftest.json·selftest.md 를 쓴다."""
    echo = echo or (lambda s: print(s, flush=True))
    checks = CHECKS if checks is None else checks
    root = Path(tempfile.mkdtemp(prefix="minedocscan-selftest-"))
    scrub = _scrubber(root)
    ctx = Ctx(root=root, publish_schema=publish_schema, keep=keep)
    started = datetime.now(UTC)
    t0 = time.monotonic()
    outcomes: list[Outcome] = []
    status: dict[str, str] = {}
    try:
        for c in checks:
            blocked = [n for n in c.needs if status.get(n) != "passed"]
            if blocked:
                o = Outcome(c.name, c.title, "skipped", 0.0, reason=f"앞의 검사({', '.join(blocked)})가 통과하지 않았다")
            else:
                t = time.monotonic()
                try:
                    o = Outcome(c.name, c.title, "passed", 0.0, detail=c.fn(ctx) or {})
                except CheckSkipped as e:
                    o = Outcome(c.name, c.title, "skipped", 0.0, reason=_line(scrub(str(e))))
                except CheckFailed as e:
                    o = Outcome(c.name, c.title, "failed", 0.0, reason=_line(scrub(str(e))))
                except Exception as e:                                # noqa: BLE001 — 어느 검사가 왜 (한 줄, 경로 없이)
                    where = traceback.extract_tb(e.__traceback__)[-1]
                    o = Outcome(c.name, c.title, "failed", 0.0, reason=_line(
                        scrub(f"{type(e).__name__}: {e}"), f" ({Path(where.filename).name}:{where.lineno})"))
                o.seconds = round(time.monotonic() - t, 2)
            outcomes.append(o)
            status[c.name] = o.status
            label = {"passed": "통과", "failed": "실패", "skipped": "건너뜀"}[o.status]
            echo(f"{label:3} {o.name:<12} {o.seconds:6.1f}초  {o.title}" + (f" — {o.reason}" if o.reason else ""))
    finally:
        for con in ctx.closers:
            try:
                con.close()
            except Exception:                                          # noqa: BLE001
                pass
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    failed = [o.name for o in outcomes if o.status == "failed"]
    blocked = [o.name for o in outcomes if o.status == "skipped" and o.reason.startswith("앞의 검사")]
    result = {
        "passed": not failed and not blocked,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.monotonic() - t0, 1),
        "failed": failed,
        "checks": [o.__dict__ for o in outcomes],
        "environment": environment(),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "selftest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "selftest.md").write_text(markdown(result), encoding="utf-8")
    if keep:
        echo(f"임시 폴더를 남겼습니다: {root}")
    return result


def markdown(result: dict) -> str:
    label = {"passed": "통과", "failed": "실패", "skipped": "건너뜀"}
    env = result["environment"]
    lines = ["# 자가 시험 결과 (minedocscan selftest)", "",
             f"- 결과: **{'통과' if result['passed'] else '실패'}**" + (f" — 실패한 검사: {', '.join(result['failed'])}"
                                                                   if result["failed"] else ""),
             f"- 시작: {result['started_at']} (UTC), 걸린 시간 {result['seconds']}초",
             f"- 판: minedocscan {env['minedocscan']}, 파이썬 {env['python']}, {env['os']} ({env['machine']}), "
             f"CPU {env['cpu_count']}개, 메모리 {env['memory_gb']} GB",
             "- 의존성: " + ", ".join(f"{k} {v}" for k, v in sorted(env["dependencies"].items())),
             "", "| 검사 | 결과 | 시간(초) | 내용 | 비고 |", "|---|---|---:|---|---|"]
    for c in result["checks"]:
        detail = ", ".join(f"{k} {v}" for k, v in c["detail"].items())
        note = (c["reason"] or detail).replace("|", "/")
        lines.append(f"| `{c['name']}` | {label[c['status']]} | {c['seconds']} | {c['title']} | {note} |")
    lines += ["", "합성 데이터만으로 돌았다 — 인식률을 말하는 것이 아니다 (정답 인식기). 경로·이름은 적지 않는다.", ""]
    return "\n".join(lines)


def valid_schema(name: str) -> bool:
    return name.startswith(SCHEMA_PREFIX) and bool(SCHEMA_RE.match(name))
