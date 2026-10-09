"""운영 화면의 처리부 (tasks/0007 4.9) — HTTP 와 무관하다 (ReviewApp 처럼 시험이 바로 부른다).

  home_json()          할 일(날짜를 정할 문서, 다시 스캔 의심 쪽, 양식 없는 쪽·정합 실패 쪽, 운영 대기열마다 남은 수), 최근 문서 50건,
                       작업 상태(쉬는 중 / 어느 문서의 몇 쪽째)
  doc_json(params)     문서 하나: 쪽 목록(양식·상태·날짜·방향), 유효한 결정, 다시 스캔 의심 쪽의 짝
  post_decision(body)  {items: [{target, kind, value, note}], confirm} — 전부 검사한 뒤에 쓴다. 받은 날보다 뒤이거나 한참 앞의
                       날짜는 한 번 되묻는다 (confirm 없이 오면 저장하지 않고 경고를 돌려준다). 저장하면 작업 스레드를 깨운다
  page_png(params)     쪽 그림 (page_id 또는 doc+page, w 로 폭) — 원본에서 그때그때 렌더링하고 방향을 알면 세운다. 몇 장만 캐시하고
                       디스크에 쓰지 않는다
  export_xlsx(kind, params)  엑셀 내려받기 (tasks/0008 4.7): 그 날짜(달)의 모델로 그때 만든다 — 폴더 설정이 없어도 된다, 디스크에 쓰지
                       않는다, 읽기는 한 트랜잭션. 달력에 없는 날짜 400, 쪽이 없는 날짜 404

운영 대기열(pending 은 양식별 · mismatch · readings · usage-check · meta-check · page-fields)의 남은 수는 build_queue(이름) 의
total − done (pending 은 total). 홈을 읽을 때마다 대기열을 통째로 만들지 않는다: DB 가 바뀔 때(쪽 처리·검수 저장·결정 저장 —
PRAGMA data_version 과 이 연결의 total_changes)만 다시 센다. 표본 대기열(haul-numbers·checks)은 홈에 두지 않는다 (만들지도 않는다).
"""
from __future__ import annotations

import sqlite3
from collections import OrderedDict

import cv2
import numpy as np

from ..imaging.align import rotate_upright
from ..imaging.io import load_page, resolve_source
from ..intake import decisions as decs
from ..store.order import row_document_key
from .queue import INPUT_KINDS, build_queue
from .server import ApiError

OPS_QUEUES = ("mismatch", "readings", "usage-check", "meta-check", "page-fields")   # + pending (양식별)
RECENT = 50
PNG_CACHE = 24                           # 쪽 그림 몇 장 (문서 화면의 미리보기 20–30장이 크롭의 원본 렌더링 캐시를 밀어내지 않게 따로)


class OpsApp:
    def __init__(self, con: sqlite3.Connection, site, settings, reviewer: str, worker=None, watching: bool = True,
                 wake=None, excel=None, publish=None):
        self.con, self.site, self.settings, self.reviewer = con, site, settings, reviewer
        self.worker, self.watching, self.wake = worker, watching, wake
        self.excel = excel                       # 자동 내보내기 (export/auto.AutoExport) — 작업 스레드가 쓴다. 화면은 상태만 읽는다
        self.publish = publish                   # 통합 DB 싣기 (publish/auto.AutoPublish) — 같은 방식
        self._counts: tuple | None = None
        self._png: OrderedDict = OrderedDict()

    # ── 홈 ─────────────────────────────────────────────────────────────────
    def remaining(self) -> dict:
        """운영 대기열마다 남은 수 — DB 가 바뀌었을 때만 다시 센다."""
        key = (self.con.execute("PRAGMA data_version").fetchone()[0], self.con.total_changes)
        if self._counts is not None and self._counts[0] == key:
            return self._counts[1]
        out: dict = {"pending": {}}
        q = ",".join("?" * len(INPUT_KINDS))
        for (t,) in self.con.execute(f"SELECT DISTINCT p.template_name FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                                     f"WHERE f.review_status = 'pending' AND f.kind IN ({q}) ORDER BY 1", INPUT_KINDS):
            n = build_queue(self.con, "pending", template=t, site=self.site)["total"]
            if n:
                out["pending"][t] = n
        for name in OPS_QUEUES:
            try:
                b = build_queue(self.con, name, site=self.site)
            except ValueError:
                continue
            out[name] = b["total"] - b["done"]
        self._counts = (key, out)
        return out

    def home_json(self) -> dict:
        con = self.con
        docs = [dict(r) for r in con.execute(
            "SELECT document_id, received_at, source_name, source_rel, source_path, n_pages, work_date, status, "
            "work_requested, work_done FROM doc_document")]
        on_day = {(r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT document_id, work_date FROM doc_page WHERE work_date IS NOT NULL")}
        for d in docs:
            d["waiting"] = d["status"] == "received" or d["work_requested"] > d["work_done"]
            d["has_day"] = (d["document_id"], d["work_date"]) in on_day
        needs_date = sorted((d for d in docs if d["status"] == "needs_date"), key=row_document_key)
        recent = sorted(docs, key=lambda d: (d["received_at"] or "", row_document_key(d)), reverse=True)[:RECENT]
        dups = [dict(r) for r in con.execute(
            "SELECT p.page_id, p.document_id, p.page_no, p.work_date, p.template_name, p.duplicate_of, p.duplicate_sim "
            "FROM doc_page p WHERE p.status = 'duplicate' ORDER BY p.work_date, p.page_id")]
        bad = dict(con.execute("SELECT status, COUNT(*) FROM doc_page WHERE status IN ('unknown_form', 'align_failed', 'error') "
                               "GROUP BY 1").fetchall())
        status = dict(self.worker.status if self.worker is not None else {"state": "off"})
        if "started" in status:                                 # 바퀴 끝의 일: 몇 초째 (시계는 작업의 것 — 화면에 보내지 않는다)
            status["elapsed_s"] = max(0, round(self.worker.clock() - status.pop("started")))
        return {"site": self.site.name, "reviewer": self.reviewer, "watching": self.watching,
                "worker": dict(status), "worker_error": getattr(self.worker, "last_error", None),
                "todo": {"needs_date": [_doc_brief(d) for d in needs_date], "duplicates": dups,
                         "unknown_form": bad.get("unknown_form", 0), "align_failed": bad.get("align_failed", 0),
                         "page_errors": bad.get("error", 0),
                         "failed_documents": sum(d["status"] == "failed" for d in docs),
                         "waiting": sum(d["waiting"] for d in docs), "queues": self.remaining(),
                         "too_long": getattr(self.worker, "too_long", 0)},
                "recent": [_doc_brief(d) for d in recent],
                "export": self.export_status(), "publish": self.publish_status(),
                "templates": {t.name: t.title for t in self.site.templates.values()}}

    def export_status(self) -> dict:
        """엑셀 자동 내보내기의 상태 (수만): 켜짐·꺼짐(이유), 마지막으로 쓴 시각·파일 수, 쓰지 못한 파일 수, 폴더가 없다."""
        if self.excel is None:
            return {"enabled": False, "reason": "no_watch" if not self.watching else "off"}
        return dict(self.excel.status)

    # ── 문서 화면 ───────────────────────────────────────────────────────────
    def doc_json(self, params: dict) -> dict:
        doc = str(params.get("id") or "")
        row = self.con.execute("SELECT * FROM doc_document WHERE document_id = ?", (doc,)).fetchone()
        if row is None:
            raise ApiError(404, f"없는 문서: {doc}")
        d = dict(row)
        d["waiting"] = d["status"] == "received" or d["work_requested"] > d["work_done"]
        d["has_day"] = self.con.execute("SELECT 1 FROM doc_page WHERE document_id = ? AND work_date = ? LIMIT 1",
                                        (doc, d["work_date"])).fetchone() is not None
        rows = {r["page_no"]: dict(r) for r in self.con.execute(
            "SELECT page_id, page_no, template_name, status, work_date, rotation, duplicate_of, duplicate_sim, error, "
            "align_grid_err FROM doc_page WHERE document_id = ? ORDER BY page_no", (doc,))}
        n = max([d["n_pages"] or 0, *rows])
        pages = [rows.get(k) or {"page_id": f"{doc}-p{k}", "page_no": k, "template_name": None, "status": None,
                                 "work_date": None, "rotation": None, "duplicate_of": None, "duplicate_sim": None,
                                 "error": None} for k in range(1, n + 1)]
        eff = decs.effective(self.con, doc)
        for p in pages:
            t = eff.page(p["page_no"])
            p["decision"] = {"date": t.date, "discarded": t.discarded, "keep": t.keep}
            if p["duplicate_of"]:
                e = self.con.execute("SELECT p.page_id, p.page_no, p.document_id, p.status, d.source_name FROM doc_page p "
                                     "JOIN doc_document d ON p.document_id = d.document_id WHERE p.page_id = ?",
                                     (p["duplicate_of"],)).fetchone()
                p["earlier"] = dict(e) if e is not None else {"page_id": p["duplicate_of"]}
        return {"document": _doc_brief(d) | {"error": d["error"], "warning": d["warning"], "date_source": d["date_source"]},
                "decision": {"date": eff.doc.date, "discarded": eff.doc.discarded},
                "pages": pages, "watching": self.watching,
                "templates": {t.name: t.title for t in self.site.templates.values()}}

    # ── 결정 ───────────────────────────────────────────────────────────────
    def post_decision(self, body: dict) -> dict:
        """결정 여럿 — 전부 검사한 뒤에 쓴다 (하나라도 틀리면 400 이고 아무것도 남지 않는다). 경고(받은 날보다 뒤·한참 앞의 날짜)가
        있으면 confirm 이 와야 저장한다."""
        if not isinstance(body, dict) or not isinstance(body.get("items"), list) or not body["items"]:
            raise ApiError(400, "본문은 {items: [{target, kind, value, note}]} 이어야 합니다")
        path = self.settings.decisions_path(self.site.root)
        try:
            check = decs.save(self.con, path, body["items"], self.reviewer, dry_run=True)
            if check["warnings"] and body.get("confirm") is not True:
                return {"ok": False, "confirm": check["warnings"]}
            out = decs.save(self.con, path, body["items"], self.reviewer)
        except decs.DecisionError as e:
            raise ApiError(400, str(e)) from e
        from ..touched import Touched

        t = Touched(documents=set(out["documents"]))            # 처리가 못 하는 문서(원본에 닿지 않는다)도 엑셀의 대기 수가 바뀐다
        for job in (self.excel, self.publish):
            if job is not None:
                job.mark(t)
        if self.wake is not None:
            self.wake.set()                                     # 작업 스레드를 깨운다 — 다음 바퀴를 기다리지 않게
        return {"ok": True, "saved": [{"decision_id": x.decision_id, "target": x.target, "kind": x.kind, "value": x.value}
                                      for x in out["decisions"]],
                "warnings": out["warnings"], "documents": out["documents"], "watching": self.watching}

    # ── 쪽 그림 ─────────────────────────────────────────────────────────────
    def page_png(self, params: dict) -> bytes:
        """쪽 그림 (PNG). page_id 또는 doc+page. w: 폭 (100–2400, 기본 900). 방향을 알면(doc_page.rotation) 세워서 준다.
        rot: 화면의 "돌려 보기" — 그 위에 시계 방향으로 더 돌린다 (0·90·180·270). 서버에서 돌려야 돌린 그림이 제 칸의 크기를 가진다
        (CSS 로 돌리면 옆 칸을 덮는다 — tasks/0008 4.10)."""
        try:
            w = int(params.get("w") or 900)
            turn = int(params.get("rot") or 0)
        except ValueError as e:
            raise ApiError(400, "w·rot 는 정수여야 합니다") from e
        if not 100 <= w <= 2400:
            raise ApiError(400, f"w 는 100–2400: {w}")
        if turn not in (0, 90, 180, 270):
            raise ApiError(400, f"rot 는 0·90·180·270: {turn}")
        if params.get("page_id"):
            doc, page_no = decs.split_target(str(params["page_id"]))
        else:
            doc = str(params.get("doc") or "")
            try:
                page_no = int(params.get("page") or 0)
            except ValueError as e:
                raise ApiError(400, "page 는 정수여야 합니다") from e
        if not page_no:
            raise ApiError(400, "page_id 또는 doc·page 가 필요합니다")
        row = self.con.execute("SELECT source_path, source_rel, n_pages FROM doc_document WHERE document_id = ?", (doc,)).fetchone()
        if row is None:
            raise ApiError(404, f"없는 문서: {doc}")
        src = resolve_source(row["source_path"], row["source_rel"], self.settings.archive_root)
        if src is None:
            raise ApiError(409, "원본에 닿지 않습니다")
        rot = self.con.execute("SELECT rotation FROM doc_page WHERE page_id = ?", (f"{doc}-p{page_no}",)).fetchone()
        rotation = ((int(rot[0]) if rot is not None and rot[0] else 0) + turn) % 360
        key = (str(src), src.stat().st_mtime_ns, page_no, w, rotation)
        if key in self._png:
            self._png.move_to_end(key)
            return self._png[key]
        dpi = 100 if w <= 1000 else self.settings.dpi              # 미리보기는 낮은 해상도로 — 렌더링이 쪽 크기의 제곱이다
        try:
            img = load_page(src, page_no, dpi, self.settings.damaged_pdf)
        except KeyError as e:
            raise ApiError(404, f"{page_no}쪽이 없습니다") from e
        except Exception as e:                                       # noqa: BLE001 — 열리지 않는 원본 (failed 문서)
            raise ApiError(409, f"원본을 열 수 없습니다: {type(e).__name__}") from e
        img = rotate_upright(img, rotation)
        h0, w0 = img.shape[:2]
        if w0 != w:
            img = cv2.resize(img, (w, max(1, round(h0 * w / w0))), interpolation=cv2.INTER_AREA if w < w0 else cv2.INTER_LINEAR)
        ok, buf = cv2.imencode(".png", np.ascontiguousarray(img))
        if not ok:
            raise ApiError(500, "그림을 만들 수 없습니다")
        png = buf.tobytes()
        self._png[key] = png
        while len(self._png) > PNG_CACHE:
            self._png.popitem(last=False)
        return png


    def publish_status(self) -> dict:
        """통합 DB 싣기의 상태: 켜짐·꺼짐(이유), 대상(호스트·DB·스키마 — 비밀번호 없이), 마지막 성공 시각, 밀린 범위의 수,
        마지막 실패의 종류."""
        if self.publish is None:
            return {"enabled": False, "reason": "no_watch" if not self.watching else "off"}
        return dict(self.publish.status)

    # ── 엑셀 내려받기 ───────────────────────────────────────────────────────
    def export_xlsx(self, kind: str, params: dict) -> tuple[bytes, str]:
        """(xlsx 바이트, 파일 이름). kind: day(date=YYYY-MM-DD) | month(month=YYYY-MM). 같은 모델로 그때 만든다 (폴더·기록 파일과 무관)."""
        import io

        from .. import __version__
        from ..export.daily import daily_book
        from ..export.model import MODEL_VERSION, is_iso_day
        from ..export.monthly import monthly_book
        from ..export.writer import MONTH_RE, now_iso
        from ..export.xlsx import write_book
        from ..store.db import read_txn

        if kind == "day":
            key = str(params.get("date") or "")
            if not is_iso_day(key):
                raise ApiError(400, "date 는 달력에 있는 YYYY-MM-DD 여야 합니다")
            with read_txn(self.con):
                if not self.con.execute("SELECT 1 FROM doc_page WHERE work_date = ? LIMIT 1", (key,)).fetchone():
                    raise ApiError(404, "그 날짜의 쪽이 없습니다")
                book = daily_book(self.con, self.site, key, self.settings.machine_values)
        else:
            key = str(params.get("month") or "")
            if not MONTH_RE.match(key):
                raise ApiError(400, "month 는 YYYY-MM 이어야 합니다")
            with read_txn(self.con):
                days = [r[0] for r in self.con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date LIKE ? "
                                                       "ORDER BY work_date", (f"{key}-%",)) if is_iso_day(r[0])]
                if not days:
                    raise ApiError(404, "그 달의 쪽이 없습니다")
                book = monthly_book(self.con, self.site, key, days, self.settings.machine_values)
        buf = io.BytesIO()
        write_book(book, buf, now_iso(), f"minedocscan {__version__} · 모델 {MODEL_VERSION}")
        return buf.getvalue(), f"{key}.xlsx"


def _doc_brief(d: dict) -> dict:
    """has_day: 그 문서의 쪽이 문서 날짜에 있다 — 엑셀 내려받기 연결은 그때만 (쪽이 다 다른 날짜로 간 문서에 404 의 연결을 두지
    않는다, tasks/0009 4.1 사)."""
    return {"document_id": d["document_id"], "received_at": d["received_at"], "source_name": d["source_name"],
            "n_pages": d["n_pages"], "work_date": d["work_date"], "status": d["status"], "waiting": d["waiting"],
            "has_day": bool(d.get("has_day"))}
