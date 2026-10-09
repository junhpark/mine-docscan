"""검수 기록의 저장과 재적용.

원본은 사이트 팩의 추가 전용 파일 `<site>/reviews/reviews.jsonl` 이다 (설정 `[paths] reviews`, `MINEDOCSCAN_REVIEWS`).
  · 한 줄 = 검수 한 건. 고치는 것도 새 줄을 추가한다. 지우거나 덮어쓰지 않는다.
  · 한 필드의 유효한 값은 가장 최근 줄(reviewed_at, 같으면 뒤의 줄)이다.
  · DB 의 doc_review 는 이 파일의 사본이다. WORK_ROOT 는 언제든 지울 수 있어야 하므로 사람이 입력한 값을 DB 에만 두지 않는다.
  · 저장 순서: 파일에 먼저 쓰고(flush) 그다음 DB.
  · 키는 field_id (`<문서 해시>-p<쪽>:<표>:<열>:<행>`). 같은 파일이면 언제 돌려도 같다.

판정(verdict)은 세 가지다.
  value      종이에 이렇게 적혀 있다 → value_final = 입력값, has_value = 1, review_status = reviewed
  empty      빈 칸이다              → value_final = '',   has_value = 0, review_status = reviewed
  illegible  읽을 수 없다           → 값은 그대로, review_status = pending (대기열과 정답에서 빠진다)

기계가 낸 값(value_raw, confidence, backend, has_value_raw, status_raw)은 검수해도 바뀌지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..forms.formats import normalize, try_normalize
from ..store.db import upsert, write_txn

VERDICTS = ("value", "empty", "illegible")


@dataclass
class Review:
    field_id: str
    verdict: str
    value: str = ""
    reviewer: str = ""
    reviewed_at: str = ""                 # ISO 8601 UTC, 초 단위. 비우면 지금
    note: str = ""
    # ── 문맥: DB 없이도 사람이 읽고 학습 데이터를 만들 수 있게 ──
    source: str = ""                      # "<파일명>#<쪽>"
    template: str = ""
    region: str = ""
    field_name: str = ""
    row_no: int = -1
    row_key: str = ""
    bbox: list[int] | None = None         # 검수 당시의 템플릿 좌표 bbox
    machine: dict = field(default_factory=dict)   # 검수 당시 기계가 낸 값: has_value, value_raw, backend, confidence
    review_id: str = ""                   # 비우면 field_id, reviewed_at, reviewer 에서 만든다

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"알 수 없는 판정 '{self.verdict}' (가능: {VERDICTS})")
        if not self.field_id or not self.reviewer:
            raise ValueError("field_id 와 reviewer 는 비울 수 없습니다")
        if self.verdict == "value" and self.value == "":
            raise ValueError("verdict=value 에는 값이 있어야 합니다 (빈 칸은 verdict=empty)")
        if self.verdict != "value":
            self.value = ""
        if not self.reviewed_at:
            self.reviewed_at = now_iso()
        if not self.review_id:
            self.review_id = make_review_id(self.field_id, self.reviewed_at, self.reviewer, self.note)

    @property
    def page_id(self) -> str:
        return self.field_id.split(":", 1)[0]

    def to_json(self) -> str:
        d = asdict(self)
        d = {"review_id": d.pop("review_id"), **d}
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> Review:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def db_row(self, seq: int) -> dict:
        x0, y0, x1, y1 = self.bbox if self.bbox else (None, None, None, None)
        m = self.machine or {}
        return {
            "review_id": self.review_id, "field_id": self.field_id, "page_id": self.page_id, "seq": seq,
            "verdict": self.verdict, "value": self.value, "reviewer": self.reviewer, "reviewed_at": self.reviewed_at,
            "note": self.note, "source": self.source, "template": self.template, "region": self.region,
            "field_name": self.field_name, "row_no": self.row_no, "row_key": self.row_key,
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "machine_has_value": m.get("has_value"), "machine_value_raw": m.get("value_raw"),
            "machine_backend": m.get("backend"), "machine_confidence": m.get("confidence"),
        }

    @classmethod
    def from_db_row(cls, r: sqlite3.Row) -> Review:
        bbox = None if r["x0"] is None else [r["x0"], r["y0"], r["x1"], r["y1"]]
        machine = {"has_value": r["machine_has_value"], "value_raw": r["machine_value_raw"],
                   "backend": r["machine_backend"], "confidence": r["machine_confidence"]}
        return cls(field_id=r["field_id"], verdict=r["verdict"], value=r["value"] or "", reviewer=r["reviewer"],
                   reviewed_at=r["reviewed_at"], note=r["note"] or "", source=r["source"] or "",
                   template=r["template"] or "", region=r["region"] or "", field_name=r["field_name"] or "",
                   row_no=-1 if r["row_no"] is None else r["row_no"], row_key=r["row_key"] or "", bbox=bbox,
                   machine=machine, review_id=r["review_id"])


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_review_id(field_id: str, reviewed_at: str, reviewer: str, note: str = "") -> str:
    """note 가 있으면 id 에 넣는다 — 같은 초에 같은 칸을 다른 검산의 확인(usage-check 의 note)으로 두 번 저장해도 두 기록이 남게.
    note 가 없으면 예전과 같은 id 다."""
    key = f"{field_id}|{reviewed_at}|{reviewer}" + (f"|{note}" if note else "")
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ── 파일 ───────────────────────────────────────────────────────────────────
def append(path: str | Path, review: Review) -> int:
    """파일 끝에 한 줄 추가하고 flush 한다. 돌려주는 값은 그 줄의 번호(1부터) = seq."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seq = 1
    if path.exists():
        with open(path, "rb") as f:
            data = f.read()
        seq = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1) + 1
        prefix = "" if (not data or data.endswith(b"\n")) else "\n"      # 쓰다 끊긴 줄 뒤에는 줄을 바꿔서 쓴다
    else:
        prefix = ""
    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + review.to_json() + "\n")
        f.flush()
    return seq


def load(path: str | Path) -> tuple[list[tuple[int, Review]], int]:
    """파일 전체를 (seq, Review) 목록으로. 깨진 줄(쓰다 끊긴 마지막 줄 등)은 건너뛰고 그 수를 같이 돌려준다."""
    path = Path(path)
    if not path.exists():
        return [], 0
    out, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for seq, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append((seq, Review.from_dict(json.loads(line))))
            except (ValueError, TypeError, KeyError):
                skipped += 1
    return out, skipped


def import_into(con: sqlite3.Connection, path: str | Path) -> dict:
    """파일 → doc_review. 같은 review_id 는 덮어쓰므로 몇 번을 읽어도 행 수가 같다(멱등)."""
    reviews, skipped = load(path)
    with write_txn(con):
        upsert(con, "doc_review", [r.db_row(seq) for seq, r in reviews])
    return {"path": str(path), "imported": len(reviews), "skipped": skipped}


# ── 조회 ───────────────────────────────────────────────────────────────────
def effective(con: sqlite3.Connection, page_id: str | None = None, field_ids: list[str] | None = None) -> dict[str, Review]:
    """필드별로 유효한(가장 최근) 검수. reviewed_at 이 같으면 파일에서 뒤에 있는 줄."""
    sql, args = "SELECT * FROM doc_review", []
    if page_id is not None:
        sql += " WHERE page_id = ?"
        args.append(page_id)
    elif field_ids is not None:
        if not field_ids:
            return {}
        sql += f" WHERE field_id IN ({','.join('?' * len(field_ids))})"
        args += list(field_ids)
    sql += " ORDER BY reviewed_at, seq"
    out: dict[str, Review] = {}
    for r in con.execute(sql, args):
        out[r["field_id"]] = Review.from_db_row(r)
    return out


def apply_verdict(row: dict, review: Review) -> dict:
    """doc_field 행에 검수를 적용한 새 행. 기계 값(value_raw, confidence, backend, has_value_raw, status_raw)은 그대로 둔다."""
    out = dict(row)
    if review.verdict == "value":
        out.update(value_final=review.value, has_value=1, review_status="reviewed")
    elif review.verdict == "empty":
        out.update(value_final="", has_value=0, review_status="reviewed")
    else:                                   # illegible: 값은 그대로, 사람이 다시 봐야 한다
        out.update(review_status="pending")
    out.update(reviewed_by=review.reviewer, reviewed_at=review.reviewed_at)
    return out


def review_from_field(con: sqlite3.Connection, field_id: str, verdict: str, value: str, reviewer: str,
                      note: str = "", reviewed_at: str = "") -> Review:
    """DB 의 doc_field 행에서 문맥(출처·양식·bbox·기계 값)을 채운 Review 를 만든다."""
    r = con.execute(
        "SELECT f.*, p.template_name, p.page_no, d.source_name FROM doc_field f "
        "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
        "WHERE f.field_id = ?", (field_id,)).fetchone()
    if r is None:
        raise KeyError(f"doc_field 에 없는 필드: {field_id}")
    return Review(field_id=field_id, verdict=verdict, value=value, reviewer=reviewer, note=note, reviewed_at=reviewed_at,
                  source=f"{r['source_name']}#{r['page_no']}", template=r["template_name"] or "", region=r["region"],
                  field_name=r["field_name"], row_no=r["row_no"], row_key=r["row_key"] or "",
                  bbox=[r["x0"], r["y0"], r["x1"], r["y1"]],
                  machine={"has_value": r["has_value_raw"], "value_raw": r["value_raw"], "backend": r["backend"],
                           "confidence": r["confidence"]})


# ── 쪽 메타 ────────────────────────────────────────────────────────────────
def field_id_of(page_id: str, name: str) -> str:
    """표 밖 자유 필드의 field_id (handlers/base.field_id 와 같은 규칙: region 'fields', row -1)."""
    return f"{page_id}:fields:{name}:-1"


def meta_from_reviews(con: sqlite3.Connection, page_id: str, template) -> dict:
    """meta_key 가 있는 자유 필드의 유효한 검수 → {meta_key: 값}. empty 는 None (라벨 값을 지운다), illegible 은 무시.
    날짜의 부분(date.month, date.day)은 검수로 받지 않는다 — 날짜는 파일명·라벨이 정한다 (tasks/0004 4.3)."""
    mf = template.review_meta_fields()
    if not mf:
        return {}
    reviews = effective(con, field_ids=[field_id_of(page_id, name) for name in mf])
    out: dict = {}
    for name, key in mf.items():
        rv = reviews.get(field_id_of(page_id, name))
        if rv is None or rv.verdict == "illegible":
            continue
        out[key] = rv.value if rv.verdict == "value" else None
    return out


def page_meta(con: sqlite3.Connection, site, source_name: str, page_no: int, page_id: str, template) -> dict:
    """쪽의 메타 중 사람·파일명에서 온 것. 우선순위: 검수값 > 페이지 라벨 > 문서 라벨 > 파일명 규칙 (tasks/0002 4.1).
    날짜는 검수로 받지 않는다. 기계가 읽은 값까지 넣은 최종 값과 출처는 doc_page_meta (pagemeta.py)."""
    meta = site.page_meta(source_name, page_no)
    for k, v in meta_from_reviews(con, page_id, template).items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    return meta


# ── 저장 ───────────────────────────────────────────────────────────────────
def field_format(site, template_name: str | None, region: str, field_name: str) -> str | None:
    """그 칸의 값의 형식 (템플릿의 format, 없으면 칸 종류의 기본). 템플릿이나 칸을 모르면 None — 형식 검사를 하지 않는다."""
    tpl = site.templates.get(template_name) if template_name else None
    return tpl.format_of(region, field_name) if tpl is not None else None


def normalized_value(site, row, review: Review) -> str:
    """verdict=value 의 값을 그 칸의 형식으로 정규화한다 (tasks/0005 4.1). 맞지 않으면 FormatError — 파일에 쓰기 전에.
    칸이 아직 DB 에 없으면(그 쪽이 아직 적재되지 않았다) 검수에 적힌 문맥(양식·표·칸)으로 형식을 찾는다."""
    if review.verdict != "value":
        return review.value
    if row is None:
        return normalize(field_format(site, review.template or None, review.region, review.field_name), review.value)
    return normalize(field_format(site, row["template_name"], row["region"], row["field_name"]), review.value)


def save(con: sqlite3.Connection, site, settings, review: Review, touched=None) -> dict:
    """검수 한 건을 저장한다: 파일 추가 → doc_review → doc_field → 핸들러의 on_review(업무 테이블·그 날짜의 교차검증)
    → 문서 상태. 파이프라인을 다시 돌리지 않아도 DB 가, 같은 파일로 처음부터 돌린 것과 같아진다 (불변식, 테스트로 고정).
    값은 그 칸의 형식으로 정규화해서 남긴다. 형식에 맞지 않으면 FormatError 이고 파일에 아무것도 쓰지 않는다.
    읽는 것부터 쓰는 트랜잭션(BEGIN IMMEDIATE) 안에서 한다 — 작업 스레드가 같은 문서를 다시 처리하는 중이어도 그 사이에 끼지 않게
    (tasks/0007 4.8). 문서 상태는 processed 와 needs_review 사이에서만 바뀐다 (received·needs_date·discarded·failed 는 그대로).
    touched(touched.Touched)를 주면 이 검수가 건드린 것을 더한다: 그 칸의 쪽의 날짜와 문서, 핸들러가 다른 쪽의 업무 행도 바꿨으면 그
    쪽의 날짜·문서 (가동 일보의 계기 연속성 — 바뀐 행의 쪽만, tasks/0009 4.2 다). 엑셀·통합 DB 가 다시 볼 범위다 (tasks/0008 4.7).
    """
    with write_txn(con):
        out = _save(con, site, settings, review, touched)
    if con.in_transaction:                                       # 부른 쪽이 열어 둔 트랜잭션도 지금처럼 커밋한다
        con.commit()
    return out


def _save(con: sqlite3.Connection, site, settings, review: Review, touched=None) -> dict:
    from ..handlers import get_handler
    from ..pipeline.runner import update_document_status

    path = settings.reviews_path(site.root)
    row = con.execute("SELECT f.*, p.template_name, p.document_id, p.page_no, d.source_name FROM doc_field f "
                      "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
                      "WHERE f.field_id = ?", (review.field_id,)).fetchone()
    review.value = normalized_value(site, row, review)          # 형식에 맞지 않으면 여기서 멈춘다 (파일에 쓰기 전)
    if row is not None and not review.source:                   # 문맥이 비어 있으면 DB 에서 채운다 (파일만 봐도 알 수 있게)
        review.source, review.template, review.region = f"{row['source_name']}#{row['page_no']}", row["template_name"] or "", row["region"]
        review.field_name, review.row_no, review.row_key = row["field_name"], row["row_no"], row["row_key"] or ""
        review.bbox = [row["x0"], row["y0"], row["x1"], row["y1"]]
        review.machine = review.machine or {"has_value": row["has_value_raw"], "value_raw": row["value_raw"],
                                            "backend": row["backend"], "confidence": row["confidence"]}
    seq = append(path, review)                                   # 파일이 원본: 먼저 쓴다
    upsert(con, "doc_review", review.db_row(seq))
    applied = False
    if row is not None:
        tpl = site.templates.get(row["template_name"])
        handler = get_handler(tpl.handler if tpl else "generic")
        frow = {k: row[k] for k in row.keys() if k not in ("template_name", "document_id", "page_no", "source_name")}
        if frow["reviewed_by"] is not None:                      # 이미 검수가 덮인 행: 기계 상태로 되돌린 뒤 적용한다
            frow.update(has_value=frow["has_value_raw"], value_final=handler.machine_final(frow))
        eff = effective(con, field_ids=[review.field_id]).get(review.field_id, review)
        upsert(con, "doc_field", apply_verdict(frow, eff))
        if row["region"] == "fields" and tpl is not None and row["field_name"] in tpl.meta_fields():
            from ..pagemeta import refresh_page

            refresh_page(con, site, row["page_id"])          # 쪽 메타(doc_page_meta)부터 — 핸들러는 그 최종 값을 읽는다
        other_pages = handler.on_review(con, site, settings, review.field_id)
        update_document_status(con, row["document_id"])
        applied = True
        if touched is not None:
            day = con.execute("SELECT work_date FROM doc_page WHERE page_id = ?", (row["page_id"],)).fetchone()
            touched.dates.update(d for d in (day[0] if day else None,) if d)
            touched.documents.add(row["document_id"])
            touched.add_pages(con, other_pages or ())
    return {"review_id": review.review_id, "seq": seq, "path": str(path), "applied": applied}


# ── 현황 ───────────────────────────────────────────────────────────────────
def stats(con: sqlite3.Connection, site=None) -> dict:
    """얼마나 했는지: 유효한 검수의 판정별·양식별·날짜별·값의 형식별 건수, 검수자별 기록 수, bbox 가 달라진 기록 수, ✓ 검수(체크 칸
    수와 행 수 — 행 하나 = 유·무 두 칸), (site 가 있으면) 분할별 건수와 날짜 수, 대기열마다 끝난 수 / 모집단 (기본 설정)."""
    eff = effective(con)
    by_verdict: dict[str, int] = {}
    by_template: dict[str, int] = {}
    by_date: dict[str, int] = {}
    bbox_changed = not_in_db = 0
    dates = dict(con.execute("SELECT page_id, work_date FROM doc_page"))
    fields = {r["field_id"]: r for r in con.execute("SELECT field_id, kind, format, x0, y0, x1, y1 FROM doc_field")}
    check_rows: set[tuple] = set()
    check_fields = 0
    by_format: dict[str, int] = {}
    for fid, rv in eff.items():
        by_verdict[rv.verdict] = by_verdict.get(rv.verdict, 0) + 1
        if fid in fields and fields[fid]["kind"].startswith("handwritten"):     # 값의 형식별 (글자 칸은 text)
            fmt = fields[fid]["format"] or "text"
            by_format[fmt] = by_format.get(fmt, 0) + 1
        by_template[rv.template or "unknown"] = by_template.get(rv.template or "unknown", 0) + 1
        d = dates.get(rv.page_id) or "unknown"
        by_date[d] = by_date.get(d, 0) + 1
        f = fields.get(fid)
        if f is None:
            not_in_db += 1
        elif rv.bbox and list(rv.bbox) != [f["x0"], f["y0"], f["x1"], f["y1"]]:
            bbox_changed += 1
        if f is not None and f["kind"] == "checkmark":
            check_fields += 1
            check_rows.add((rv.page_id, rv.region, rv.row_no))
    by_reviewer = dict(con.execute("SELECT reviewer, COUNT(*) FROM doc_review GROUP BY 1 ORDER BY 1"))
    by_split: dict[str, dict] = {}
    if site is not None:
        for rv, sp, d in effective_with_split(con, site):
            if rv.verdict == "illegible":
                continue
            g = by_split.setdefault(sp, {"fields": 0, "dates": set()})
            g["fields"] += 1
            g["dates"].add(d)
        by_split = {k: {"fields": v["fields"], "dates": len(v["dates"])} for k, v in sorted(by_split.items())}
    return {"records": con.execute("SELECT COUNT(*) FROM doc_review").fetchone()[0], "fields": len(eff),
            "by_verdict": dict(sorted(by_verdict.items())), "by_template": dict(sorted(by_template.items())),
            "by_date": dict(sorted(by_date.items())), "by_reviewer": by_reviewer,
            "bbox_changed": bbox_changed, "fields_not_in_db": not_in_db, "by_split": by_split,
            "checks": {"fields": check_fields, "rows": len(check_rows)}, "by_format": dict(sorted(by_format.items())),
            "by_queue": _queue_progress(con, site) if site is not None else {}}


def _queue_progress(con: sqlite3.Connection, site) -> dict:
    from .queue import queue_progress

    return queue_progress(con, site)


def effective_with_split(con: sqlite3.Connection, site) -> list[tuple[Review, str, str | None]]:
    """유효한 검수마다 (검수, 분할, 날짜). 분할은 쪽의 날짜로 정한다 (evaluate/split.py). 쪽이 DB 에 없으면 unknown."""
    dates = dict(con.execute("SELECT page_id, work_date FROM doc_page"))
    out = []
    for rv in effective(con).values():
        d = dates.get(rv.page_id)
        out.append((rv, site.split_of(d) if site is not None else "unknown", d))
    return out


def export_answers(con: sqlite3.Connection, out: str | Path, split: str = "all", site=None) -> int:
    """유효한 검수(value, empty) → answers.json (recognize.load_answers_json 형식). illegible 은 뺀다.
    split: all | test | train — 날짜 분할 (site 필요). 표본만 검수했다면 `eval --only-listed` 로 비교한다."""
    if split != "all" and site is None:
        raise ValueError("--split 에는 사이트 팩이 필요합니다")
    items = []
    fmts = dict(con.execute("SELECT field_id, format FROM doc_field"))
    for rv, sp, _d in effective_with_split(con, site):
        if rv.verdict == "illegible" or not rv.source or (split != "all" and sp != split):
            continue
        # 정규화한 표기로 (칸의 형식 — 예전 검수 줄은 정규화 전일 수 있다. 맞지 않으면 그대로 둔다)
        text = try_normalize(fmts.get(rv.field_id), rv.value) if rv.verdict == "value" else ""
        items.append({"source": rv.source, "template": rv.template, "region": rv.region, "field_name": rv.field_name,
                      "row_key": rv.row_key, "text": text})
    items.sort(key=lambda a: (a["source"], a["template"], a["region"], a["row_key"], a["field_name"]))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(items)
