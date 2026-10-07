"""파이프라인 실행기.

  등록      파일 해시로 문서 등록(같은 바이트는 한 문서), 한 번 열어 쪽 수 — 열리지 않으면 failed (register)
  처리      그 문서가 만든 것을 지우고 결정을 적용해 다시 만든다 (process_document — tasks/0007 4.1, 4.8):
    classify  페이지가 어느 양식인지 (그날 유효한 판만 — 날짜가 먼저다)
    align     양식 기준 이미지로 정합, 품질 점수 (돌아간 쪽은 세워서 다시 — imaging/align.align_upright)
    duplicate 같은 날·같은 계열의 앞 순서 적재된 쪽과 손글씨 자리의 서명이 비슷하면 붙잡는다 (다시 스캔한 쪽 — 4.6, imaging/signature.py)
    extract   셀 크롭·잉크 관측
    recognize → correct → validate → load   — 양식의 핸들러가 수행 (handlers/)
    finalize  그 문서가 있던·있는 날짜와 장비의 교차검증·연속성·점검 행 (등록된 핸들러 전부)

이 파일은 단계 순서와 상태 기록만 안다. 양식의 의미는 핸들러가, 글자 읽기는 인식 백엔드가 안다.
시작할 때 사이트 팩의 검수 파일(doc_review)과 결정 파일(doc_decision)을 읽어 들이므로, DB 를 지우고 다시 돌려도 사람이 입력한 값과
정한 것(날짜·버리기)은 다시 붙는다.

문서의 자리 (doc_document.status — tasks/0007 4.1):
  received      등록했고 처리가 아직 끝나지 않았다 (새 문서, 또는 처리하다 끊긴 문서)
  needs_date    날짜를 얻지 못했다 (문서의 결정 > 문서 라벨(ISO) > 파일명 규칙). 사람이 정할 때까지 기다린다 — 쪽·필드·업무 행 없음
  processed · needs_review   처리했다 (검수 대기 필드나 양식·정합·쪽 오류·다시 스캔 의심 쪽이 있으면 needs_review)
  failed        읽지 못했다       discarded   사람이 버렸다
다시 처리 대기는 상태가 아니라 요청 번호다: work_requested(요청마다 +1) > work_done(처리를 시작할 때 읽은 번호를 끝날 때 적는다).
처리하는 도중에 온 요청은 남아서 다음에 다시 처리된다. 대기 중인 문서는 문서의 순서(store/order.py)대로 처리한다.

트랜잭션 (4.8): 쓰는 단위는 BEGIN IMMEDIATE (store.db.write_txn). 문서를 지우는 것 하나, 쪽마다 하나, 마무리 하나 — 쪽 하나가 끝날
때마다 커밋하므로 화면의 저장이 쪽 하나만큼만 기다린다. 지울 때 그 문서가 있던 날짜·장비를 바로 다시 계산해 두므로 어느 커밋 뒤에
끊겨도 DB 는 처리 중인 그 문서만 빼고 맞다. 문서를 읽다 실패하면 그 문서의 행을 지우고 failed 로 남긴다.

전체 묶음(수백 파일)을 돌릴 때 (tasks/0002 4.2, 4.3):
  · 한 문서의 실패가 전체를 멈추지 않는다. 문서를 읽지 못하면 그 문서는 failed, 쪽 하나에서 예외가 나면 그 쪽만 error(SAVEPOINT).
    strict=True 면 첫 오류에서 멈춘다(디버깅용). 실패는 summary["failed"] 에 모인다.
  · skip_existing=True 면 끝까지 처리된 문서(processed·needs_review, 쪽 오류 없음, 대기 중 아님)와 버린 문서를 건너뛴다.
    needs_date 는 날짜만 다시 본다. failed 는 다시 한다. 템플릿이나 인식기를 바꾼 뒤에는 처음부터 다시 돌린다.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .. import pagemeta
from ..config import Settings
from ..correct import Corrector, get_corrector
from ..forms.classify import FormClassifier
from ..forms.sitepack import SitePack
from ..handlers import REGISTRY, PageContext, get_handler
from ..handlers.base import apply_reviews, field_row
from ..imaging import signature as sigs
from ..imaging.align import align_to_template, align_upright
from ..imaging.cells import observe_cells, page_ink
from ..imaging.cropspec import PageImages, crop_cell
from ..imaging.grid import binarize
from ..imaging.io import IMAGE_EXT, SUPPORTED_EXT, count_pages, imwrite, load_pages, resolve_source
from ..intake import decisions as decs
from ..recognize import Recognizer, build_recognizer
from ..recognize.meta.model import build_meta_readers
from ..review.store import import_into
from ..store.db import delete_pages, open_db, upsert, write_txn
from ..store.order import document_id as document_id_of
from ..store.order import row_document_key
from ..touched import Touched
from ..validate.usage import equipment_ref

PENDING_SQL = "status = 'received' OR work_requested > work_done"     # 다시 처리 대기 (4.1)


class Pipeline:
    def __init__(self, settings: Settings, site: SitePack | None = None, recognizer: Recognizer | None = None,
                 corrector: Corrector | None = None, con: sqlite3.Connection | None = None, load_reviews: bool = True,
                 meta_readers: dict | None = None, load_decisions: bool = True):
        """load_reviews=False 면 검수 파일을 읽어 들이지 않는다 — 회귀 검사처럼 기계 값만 봐야 할 때.
        load_decisions: 결정 파일(날짜·버리기)은 기본으로 읽는다 — 회귀 검사도 읽는다 (무엇을 적재하는가의 일부, tasks/0007 4.3).
        meta_readers: 메타 필드 모델 {키: MetaModel}. None 이면 설정([recognize.meta])에서 만든다."""
        self.settings = settings
        if site is None:
            if settings.site is None:
                raise ValueError("사이트 팩 경로가 없습니다. 설정의 [paths] site 또는 MINEDOCSCAN_SITE 를 지정하세요.")
            site = SitePack(settings.site)
        self.site = site
        self.recognizer = recognizer or build_recognizer(settings, site)
        self.corrector = corrector or get_corrector(settings.corrector)
        # 표 밖 메타 필드(차량번호·작성자·날짜의 월·일)의 모델 — [recognize.meta]. 없으면 지금처럼 읽지 않는다 (tasks/0004 단계 5)
        self.meta_readers = build_meta_readers(settings, site) if meta_readers is None else meta_readers
        self._meta_candidates: dict[str, list[str]] = {}
        self.con = con or open_db(settings.resolved_db_url)
        self.classifier = FormClassifier(list(site.templates.values()))
        self._handlers: dict[str, object] = {}
        self.summary: dict = {"documents": 0, "pages": 0, "by_form": {}, "by_status": {}, "low_margin": [],
                              "handlers": {}, "skipped": 0, "failed": [], "page_errors": [], "warnings": [],
                              "needs_date": [], "unreachable": [], "discarded": [], "duplicates": []}
        self.summary["reviews"] = (import_into(self.con, settings.reviews_path(self.site.root)) if load_reviews
                                   else {"path": None, "imported": 0, "skipped": 0})
        self.summary["decisions"] = (decs.import_into(self.con, settings.decisions_path(self.site.root)) if load_decisions
                                     else {"path": None, "imported": 0, "skipped": 0})
        self.on_page = None             # 쪽 하나를 커밋한 뒤 부르는 훅 (document_id, page_no) — 시험과 화면의 작업 상태
        self.on_document = None         # 문서 하나의 처리를 시작할 때 (document_id) — 화면의 작업 상태
        # 처리가 건드린 것 (날짜·문서·장비 — tasks/0008 4.7): 작업 스레드가 바퀴의 끝에 가져가 엑셀·통합 DB 에 다시 볼 범위로 쓴다
        self.touched = Touched()

    # ── 입력 ───────────────────────────────────────────────────────────────
    @staticmethod
    def expand(paths: list[str | Path]) -> list[Path]:
        """파일과 폴더를 받아 처리할 파일 목록으로 푼다 (이름순)."""
        out: list[Path] = []
        for p in map(Path, paths):
            if p.is_dir():
                out += sorted(q for q in p.rglob("*") if q.suffix.lower() in SUPPORTED_EXT)
            elif p.suffix.lower() in SUPPORTED_EXT:
                out.append(p)
        return out

    def run(self, paths: list[str | Path], template: str | None = None, skip_existing: bool = False,
            strict: bool = False) -> dict:
        """준 경로를 다 돈 뒤 대기 중인 문서(받기만 했거나 다시 처리를 요청받은 문서)를 문서의 순서대로 마저 처리하고, 마무리한다."""
        for f in self.expand(paths):
            self.process_file(f, template=template, skip_existing=skip_existing, strict=strict)
        self.process_pending(template=template, strict=strict)
        return self.finalize()

    # ── 문서 하나 ──────────────────────────────────────────────────────────
    def process_file(self, path: str | Path, template: str | None = None, skip_existing: bool = False,
                     strict: bool = False) -> dict:
        """등록하고 바로 처리한다. 돌려주는 값: {"document_id", "status": ok | failed | needs_date | discarded | unreachable,
        "pages", "warning" | "error" | "skipped"}."""
        path = Path(path)
        try:
            document_id = document_id_of(path.read_bytes())
        except OSError as e:
            if strict:
                raise
            document_id = "unreadable-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:5]   # 읽지도 못하면 경로로
            return self._fail_document(document_id, path, path.stem, e)
        if skip_existing and self._can_skip(document_id):
            self.summary["skipped"] += 1
            row = self._doc(document_id)
            return {"document_id": document_id, "status": row["status"], "skipped": True, "pages": []}
        reg = self.register(path, document_id=document_id, strict=strict)
        if reg["status"] == "failed":
            return {"document_id": document_id, "status": "failed", "error": reg["error"], "pages": []}
        return self.process_document(document_id, template=template, strict=strict)

    def _can_skip(self, document_id: str) -> bool:
        """skip_existing: 끝까지 처리했고 쪽 오류가 없고 대기 중이 아닌 문서, 버린 문서. needs_date 는 지금도 날짜가 없을 때만."""
        row = self._doc(document_id)
        if row is None or row["status"] == "received" or row["work_requested"] > row["work_done"]:
            return False
        if row["status"] in ("processed", "needs_review"):
            n_err = con_count(self.con, "SELECT COUNT(*) FROM doc_page WHERE document_id = ? AND status = 'error'", document_id)
            return n_err == 0
        if row["status"] == "discarded":
            return True
        if row["status"] == "needs_date":
            return self.document_date(row["source_name"], decs.effective(self.con, document_id))[0] is None
        return False

    def _doc(self, document_id: str):
        return self.con.execute("SELECT * FROM doc_document WHERE document_id = ?", (document_id,)).fetchone()

    def register(self, path: str | Path, document_id: str | None = None, strict: bool = False,
                 source_name: str | None = None, received_at: str | None = None, source_rel: str | None = None) -> dict:
        """등록: 해시, 한 번 열어 쪽 수 (tasks/0007 4.1). 열리지 않는 파일(쓰레기 바이트, 쪽이 없는 PDF, 손상 방침에 걸린 PDF)은
        날짜와 상관없이 failed. 이미 있는 문서면 경로·이름만 고치고 상태·요청 번호·받은 시각은 그대로 둔다 (failed 였으면 received).
        source_name·received_at·source_rel: 접수(intake/inbox.py)가 원래 파일명과 받은 시각, 보관 경로를 준다.
        돌려주는 값: {"document_id", "status": new | known | failed, "error"}."""
        path = Path(path)
        source_name = source_name or path.stem
        if document_id is None:
            document_id = document_id_of(path.read_bytes())
        warnings: list[str] = []
        try:
            n_pages = count_pages(path, self.settings.damaged_pdf, warnings)
        except Exception as e:                            # noqa: BLE001 — 열리지 않는 파일은 그 문서만 failed
            if strict:
                raise
            out = self._fail_document(document_id, path, source_name, e, source_rel=source_rel, received_at=received_at)
            return {"document_id": document_id, "status": "failed", "error": out["error"]}
        old = self._doc(document_id)
        row = self._document_row(document_id, path, source_name, "received", None, source_rel=source_rel)
        row.update(n_pages=n_pages, warning="; ".join(warnings) or None, received_at=received_at or _received_now(row["source_rel"]))
        if old is not None and old["status"] != "failed":
            row["status"] = old["status"]
        with write_txn(self.con):
            upsert(self.con, "doc_document", row, insert_only=("received_at",))
        return {"document_id": document_id, "status": "known" if old is not None else "new", "error": None}

    def document_date(self, source_name: str, dec: decs.DocDecisions | None = None) -> tuple[str | None, str | None]:
        """문서의 날짜와 출처 (4.2): 문서의 결정 > 문서 라벨(ISO 만) > 파일명 규칙. 없으면 (None, None) — needs_date."""
        return pagemeta.document_date(self.site, source_name, dec.doc.date if dec else None)

    def pending_documents(self) -> list[str]:
        """다시 처리 대기 중인 문서 (received 이거나 요청 번호가 처리한 번호보다 크다) — 문서의 순서대로."""
        rows = self.con.execute(f"SELECT document_id, source_rel, source_path FROM doc_document WHERE {PENDING_SQL}").fetchall()
        return [r["document_id"] for r in sorted(rows, key=row_document_key)]

    def process_pending(self, template: str | None = None, strict: bool = False, limit: int | None = None,
                        catch: bool = False) -> int:
        """대기 중인 문서를 문서의 순서대로 처리한다 — 처리가 뒤 문서에 요청을 남기면 그것까지 (요청은 뒤로만 가므로 끝난다).
        원본에 닿지 않는 문서는 이번에는 건너뛴다 (다음에 다시 본다). catch=True(작업 스레드)면 처리 밖에서 난 예외(DB 가 잠겼다 …)도
        그 문서를 failed 로 남기고 다음 문서로 간다 — 작업 스레드가 죽지 않는다 (tasks/0007 4.9). 돌려주는 값: 처리한 문서 수."""
        done, skip = 0, set()
        while limit is None or done < limit:
            todo = [d for d in self.pending_documents() if d not in skip]
            if not todo:
                break
            try:
                out = self.process_document(todo[0], template=template, strict=strict)
            except Exception as e:                        # noqa: BLE001 — catch 일 때만 (아니면 그대로 올린다)
                if not catch:
                    raise
                self.fail_processing(todo[0], e)
                skip.add(todo[0])
                done += 1
                continue
            if out["status"] == "unreachable":
                skip.add(todo[0])
            else:
                done += 1
        return done

    def fail_processing(self, document_id: str, e: BaseException) -> None:
        """처리하다 난 예외(문서를 읽는 것 밖 — DB 잠김 등): 열린 트랜잭션을 되돌리고 그 문서의 행을 지워 failed, work_done 을 적는다.
        그것마저 안 되면(DB 가 계속 잠겨 있다) 그대로 둔다 — 대기 중인 채로 다음 바퀴에 다시 본다."""
        if self.con.in_transaction:
            self.con.rollback()
        err = _error_text(e)
        self.touched.documents.add(document_id)
        try:
            with write_txn(self.con):
                before = self._clear_document(document_id)
                self._request_later(document_id, before.groups)   # 지운 쪽 때문에 붙잡혀 있던 뒤 문서 (4.6 ①)
                self.con.execute("UPDATE doc_document SET status = 'failed', error = ?, work_done = work_requested "
                                 "WHERE document_id = ?", (err, document_id))
        except sqlite3.Error:
            return
        self.summary["failed"].append({"document_id": document_id, "source_name": None, "error": err})

    def process_document(self, document_id: str, template: str | None = None, strict: bool = False) -> dict:
        """처리 (4.8): ① 원본을 찾는다 (닿지 않으면 건드리지 않고 다음에) ② 그 문서가 만든 것을 지운다 — 있던 날짜·장비를 바로 다시
        계산해 두고, 상태는 received ③ 버린 문서면 discarded, 날짜가 없으면 needs_date ④ 쪽마다: 결정 → 분류 → 정합 → 빈 쪽 →
        핸들러 (쪽마다 커밋) ⑤ 그 문서가 있는 날짜·장비를 다시 계산하고 상태와 work_done 을 적는다."""
        row = self._doc(document_id)
        if row is None:
            raise KeyError(f"등록되지 않은 문서입니다: {document_id}")
        dec = decs.effective(self.con, document_id)
        src = resolve_source(row["source_path"], row["source_rel"], self.settings.archive_root)
        if src is None and not dec.doc.discarded:
            self.summary["unreachable"].append(document_id)
            return {"document_id": document_id, "status": "unreachable", "pages": []}
        if self.on_document:
            self.on_document(document_id)
        self.touched.documents.add(document_id)
        snapshot = copy.deepcopy(self.summary)            # 문서가 실패하면 그 문서의 집계도 되돌린다
        with write_txn(self.con):                         # ② 요청 번호는 결정을 읽기 전에 (4.1)
            req = self._doc(document_id)["work_requested"]
            before = self._clear_document(document_id)
        dec = decs.effective(self.con, document_id)
        source_name = row["source_name"]
        doc_date, date_source = self.document_date(source_name, dec)
        if dec.doc.discarded:
            self._finish_document(document_id, req, before, status="discarded", date=(doc_date, date_source))
            self.summary["discarded"].append(document_id)
            return {"document_id": document_id, "status": "discarded", "pages": []}
        if doc_date is None:
            self._finish_document(document_id, req, before, status="needs_date", date=(None, None))
            self.summary["needs_date"].append({"document_id": document_id, "source_name": source_name})
            return {"document_id": document_id, "status": "needs_date", "pages": []}
        warnings: list[str] = []
        pages = []
        try:
            for page_no, gray in load_pages(src, self.settings.dpi, self.settings.damaged_pdf, warnings):
                if dec.page(page_no).discarded:
                    pages.append(self._discarded_page(document_id, source_name, page_no, dec))
                else:
                    pages.append(self.process_page(document_id, source_name, page_no, gray, template, strict=strict,
                                                   source_path=src, decided=dec))
                if self.on_page:
                    self.on_page(document_id, page_no)
        except Exception as e:                            # noqa: BLE001 — 한 문서의 실패가 전체를 멈추지 않는다
            if strict:
                raise
            if self.con.in_transaction:
                self.con.rollback()
            self.summary = snapshot
            with write_txn(self.con):
                after = self._clear_document(document_id)
            self._finish_document(document_id, req, before | after, status="failed", error=_error_text(e))
            self.summary["failed"].append({"document_id": document_id, "source_name": source_name,
                                           "error": _error_text(e)})
            return {"document_id": document_id, "status": "failed", "error": _error_text(e), "pages": []}
        warning = "; ".join(warnings) or None
        self._finish_document(document_id, req, before, status=None, date=(doc_date, date_source), n_pages=len(pages),
                              warning=warning)
        with write_txn(self.con):                         # 같은 경로의 옛 failed 기록(깨졌던 바이트의 해시)은 이 성공이 대체한다
            old = [r[0] for r in self.con.execute("SELECT document_id FROM doc_document WHERE source_path = ? AND "
                                                  "status = 'failed' AND document_id <> ?", (row["source_path"], document_id))]
            self.con.execute("DELETE FROM doc_document WHERE source_path = ? AND status = 'failed' AND document_id <> ?",
                             (row["source_path"], document_id))
        self.touched.removed.update(old)                  # 싣기가 대상에서도 지운다 (tasks/0008 4.7)
        self.summary["documents"] += 1
        if warning:                                       # 경고만으로는 종료 코드가 1 이 되지 않는다
            self.summary["warnings"].append({"document_id": document_id, "source_name": source_name, "warning": warning})
        return {"document_id": document_id, "status": "ok", "pages": pages, "warning": warning}

    def _footprint(self, document_id: str) -> Footprint:
        """그 문서의 쪽이 지금 있는 날짜·장비·쪽, 다시 스캔을 견주는 묶음(날짜, 계열) (지우기 전과 처리한 뒤 — 다시 계산할 범위)."""
        pages = self.con.execute("SELECT page_id, work_date, template_name FROM doc_page WHERE document_id = ?",
                                 (document_id,)).fetchall()
        refs = {equipment_ref(r) for r in self.con.execute(
            "SELECT u.* FROM eq_usage_daily u JOIN doc_page p ON u.page_id = p.page_id WHERE p.document_id = ?",
            (document_id,)).fetchall()}
        groups = {(r["work_date"], self._sig_family(r["template_name"])) for r in pages if r["work_date"] and r["template_name"]}
        return Footprint({r["work_date"] for r in pages if r["work_date"]}, {r for r in refs if r}, {r["page_id"] for r in pages},
                         groups)

    def _sig_family(self, template_name: str) -> str:
        tpl = self.site.templates.get(template_name)
        return tpl.sig_family if tpl is not None else template_name

    def _clear_document(self, document_id: str) -> Footprint:
        """② 그 문서가 만든 것을 지우고(store.db.PAGE_TABLES) 있던 날짜·장비를 다시 계산한다 — 부른 쪽의 트랜잭션 안에서.
        doc_review·doc_decision 은 지우지 않는다. 상태는 received (끊기면 다시 처리된다). 돌려주는 값: 지우기 전의 범위."""
        before = self._footprint(document_id)
        delete_pages(self.con, sorted(before.pages))
        self.con.execute("UPDATE doc_document SET status = 'received', error = NULL WHERE document_id = ?", (document_id,))
        if before.pages:
            self.finalize(dates=before.dates, equipment=before.refs, commit=False)
        return before

    def _finish_document(self, document_id: str, req: int, before: Footprint, status: str | None,
                         date: tuple[str | None, str | None] = (None, None), error: str | None = None,
                         n_pages: int | None = None, warning: str | None = None) -> None:
        """⑤ 그 문서가 지금 있는 날짜·장비를 다시 계산하고 상태·날짜·work_done 을 적는다 (한 트랜잭션). 지우기 전의 날짜·장비는
        ②에서 이미 다시 계산했다 (before 는 기록용). status=None 이면 처리한 결과로 (processed | needs_review)."""
        with write_txn(self.con):
            after = self._footprint(document_id)
            if after.pages:
                self.finalize(dates=after.dates, equipment=after.refs, commit=False)
            self._request_later(document_id, before.groups | after.groups)
            sets = {"work_date": date[0], "date_source": date[1], "work_done": req}
            if status is not None:
                sets.update(status=status, error=error)
            if n_pages is not None:
                sets.update(n_pages=n_pages, warning=warning)
            self.con.execute(f"UPDATE doc_document SET {', '.join(f'{k} = ?' for k in sets)} WHERE document_id = ?",
                             (*sets.values(), document_id))
            if status is None:
                self.con.execute("UPDATE doc_document SET status = ? WHERE document_id = ?",
                                 (document_status(self.con, document_id), document_id))

    def _request_later(self, document_id: str, groups: set[tuple[str, str]]) -> list[str]:
        """다시 스캔의 판정은 앞 순서의 적재된 쪽만 본다 (4.6). 이 문서를 처리했으니(버리기·날짜 바꾸기 포함) 판정이 달라질 수 있는
        뒤 순서의 문서에 다시 처리를 요청한다: ① 이 문서의 쪽(지우기 전의 것 포함)과 같은 날짜·계열에 붙잡힌 쪽이 있는 문서,
        ② 이 문서의 적재된 쪽과 같은 날짜·계열이고 서명이 기준 이상으로 비슷한 적재된 쪽(keep 이 없는 것)이 있는 문서.
        요청은 뒤로만 가므로 끝난다. 돌려주는 값: 요청한 문서."""
        if not groups:
            return []
        mine = self._doc_key(document_id)
        my_sigs: dict[tuple[str, str], list] = {}
        for r in self.con.execute("SELECT work_date, family, sig FROM doc_page_sig WHERE document_id = ?", (document_id,)):
            my_sigs.setdefault((r["work_date"], r["family"]), []).append(sigs.decode(r["sig"]))
        found: set[str] = set()
        for day, fam in sorted(groups):
            for r in self.con.execute("SELECT document_id, template_name FROM doc_page WHERE status = 'duplicate' AND "
                                      "work_date = ? AND document_id <> ?", (day, document_id)):
                if self._sig_family(r["template_name"]) == fam:
                    found.add(r["document_id"])                                       # ①
            mine_here = my_sigs.get((day, fam))
            if not mine_here:
                continue
            for r in self.con.execute("SELECT s.page_id, s.document_id, s.sig, p.page_no FROM doc_page_sig s JOIN doc_page p "
                                      "ON s.page_id = p.page_id WHERE s.work_date = ? AND s.family = ? AND s.document_id <> ?",
                                      (day, fam, document_id)):
                if r["document_id"] in found:
                    continue
                other = sigs.decode(r["sig"])
                if any((sigs.similarity(m, other) or 0.0) >= self.settings.dup_min_sim for m in mine_here) \
                        and not decs.effective(self.con, r["document_id"]).page(r["page_no"]).keep:
                    found.add(r["document_id"])                                       # ②
        later = sorted(d for d in found if self._doc_key(d) > mine)
        decs.request_work(self.con, later)
        return later

    def _doc_key(self, document_id: str) -> tuple:
        r = self.con.execute("SELECT document_id, source_rel, source_path FROM doc_document WHERE document_id = ?",
                             (document_id,)).fetchone()
        return row_document_key(r) if r is not None else (1, (), document_id)

    def _earlier_match(self, page: dict, family: str, sig) -> tuple[str, float] | None:
        """같은 날짜·계열의 앞 순서 적재된 쪽 중 서명이 가장 비슷한 것 (쪽 ID, 유사도). 견줄 것이 없으면 None.
        같은 유사도면 앞 순서의 쪽."""
        mine = (self._doc_key(page["document_id"]), page["page_no"])
        rows = self.con.execute("SELECT s.page_id, s.sig, p.page_no, d.document_id, d.source_rel, d.source_path FROM doc_page_sig s "
                                "JOIN doc_page p ON s.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
                                "WHERE s.work_date = ? AND s.family = ? AND s.page_id <> ?",
                                (page["work_date"], family, page["page_id"])).fetchall()
        best = None
        for key, r in sorted(((row_document_key(r), r["page_no"]), r) for r in rows):   # 앞 순서부터 — 같은 유사도면 앞의 것
            if key >= mine:
                break
            sim = sigs.similarity(sig, sigs.decode(r["sig"]))
            if sim is not None and (best is None or sim > best[1]):
                best = (r["page_id"], sim)
        return best

    def _discarded_page(self, document_id: str, source_name: str, page_no: int, dec: decs.DocDecisions) -> dict:
        """버린 쪽: 행 하나만 status discarded 로 (필드·업무 행 없음)."""
        d, _src = pagemeta.page_date(self.site, source_name, page_no, dec.page_date(page_no))
        page = _page_row(f"{document_id}-p{page_no}", document_id, page_no, d)
        page["status"] = "discarded"
        with write_txn(self.con):
            return self._close_page(page)

    def _document_row(self, document_id: str, path: Path, source_name: str, status: str, error: str | None,
                      source_rel: str | None = None) -> dict:
        rel = source_rel
        if rel is None and self.settings.archive_root is not None:
            try:
                rel = path.resolve().relative_to(Path(self.settings.archive_root).resolve()).as_posix()
            except ValueError:
                rel = None
        work_date, date_source = self.document_date(source_name, decs.effective(self.con, document_id))
        return {"document_id": document_id, "source_path": str(path), "source_rel": rel, "source_name": source_name,
                "work_date": work_date, "date_source": date_source, "n_pages": None, "status": status,
                "error": error, "warning": None, "created_at": datetime.now(UTC).isoformat(timespec="seconds")}

    def _fail_document(self, document_id: str, path: Path, source_name: str, e: BaseException,
                       source_rel: str | None = None, received_at: str | None = None) -> dict:
        """읽지 못한 문서: 그 문서의 행을 지우고(있었다면 — 그 날짜·장비를 다시 계산) failed, work_done = work_requested."""
        err = _error_text(e)
        self.touched.documents.add(document_id)
        row = self._document_row(document_id, path, source_name, "failed", err, source_rel=source_rel)
        row["received_at"] = received_at or _received_now(row["source_rel"])
        with write_txn(self.con):
            before = self._clear_document(document_id)
            self._request_later(document_id, before.groups)       # 지운 쪽 때문에 붙잡혀 있던 뒤 문서 (4.6 ①)
            upsert(self.con, "doc_document", row, insert_only=("received_at",))
            self.con.execute("UPDATE doc_document SET status = 'failed', error = ?, work_done = work_requested "
                             "WHERE document_id = ?", (err, document_id))
        self.summary["failed"].append({"document_id": document_id, "source_name": source_name, "error": err})
        return {"document_id": document_id, "status": "failed", "error": err, "pages": []}

    # ── 페이지 하나 ────────────────────────────────────────────────────────
    def process_page(self, document_id: str, source_name: str, page_no: int, gray: np.ndarray,
                     template: str | None = None, strict: bool = False, source_path: Path | None = None,
                     decided: decs.DocDecisions | None = None) -> dict:
        """쪽 하나 — 쓰는 트랜잭션 하나 (열린 트랜잭션이 없으면 BEGIN IMMEDIATE … COMMIT). 예외가 나면 그 쪽의 행만 되돌리고
        (SAVEPOINT) status=error 로 남긴다. strict 면 되돌린 뒤 그대로 올린다. decided: 그 문서의 결정 (쪽의 날짜)."""
        own = not self.con.in_transaction
        if own:
            self.con.execute("BEGIN IMMEDIATE")           # 쪽 하나가 한 트랜잭션 (tasks/0007 4.8)
        self.con.execute("SAVEPOINT page")
        snapshot = copy.deepcopy(self.summary)
        page_id = f"{document_id}-p{page_no}"
        work_date, _src = pagemeta.page_date(self.site, source_name, page_no,
                                             decided.page_date(page_no) if decided is not None else None)
        page = _page_row(page_id, document_id, page_no, work_date)
        try:
            out = self._process_page(page, source_name, gray, template, source_path, decided)
            self.con.execute("RELEASE SAVEPOINT page")
        except Exception as e:                            # noqa: BLE001
            self.con.execute("ROLLBACK TO SAVEPOINT page")
            self.con.execute("RELEASE SAVEPOINT page")
            if strict:
                if own:
                    self.con.rollback()
                raise
            self.summary = snapshot
            err = _error_text(e)
            self.summary["page_errors"].append({"page_id": page_id, "error": err})
            page.update(status="error", error=err)        # 어디까지 갔는지(양식·정합)는 남긴다
            out = self._close_page(page)
        if own:
            self.con.commit()
        return out

    def _process_page(self, page: dict, source_name: str, gray: np.ndarray, template: str | None = None,
                      source_path: Path | None = None, decided: decs.DocDecisions | None = None) -> dict:
        s = self.summary
        page_id, document_id, page_no = page["page_id"], page["document_id"], page["page_no"]
        day = page["work_date"]                     # 쪽의 날짜 (pagemeta.page_date) — 분류보다 먼저 필요하다 (ADR 0010)

        # classify
        group = None
        if template:
            name, margin = template, None
        else:
            cands = [t.name for t in self.site.templates_for(day)]     # 그날 유효한 판만 (tasks/0002 4.4)
            groups = self.site.concurrent_groups(day)                 # 같은 날 섞여 쓰이는 판 (tasks/0006 4.6)
            cr = self.classifier.classify(gray, candidates=cands, groups=groups)
            name, margin, group = cr.template, cr.margin, cr.group
            if name and margin < self.settings.classify_min_margin:
                s["low_margin"].append({"page_id": page_id, "template": name, "margin": round(margin, 2)})
        page["template_name"], page["classify_margin"] = name, margin
        tpl = self.site.templates.get(name) if name else None
        if tpl is None:
            # 빈 쪽 (tasks/0007 4.5): 양식을 못 찾은 쪽 중 어두운 화소가 거의 없는 것 — 양면 스캔의 뒷면. 양식을 찾은 쪽은 아무리
            # 옅어도 빈 쪽이 아니다. blank 는 문서를 needs_review 로 만들지 않는다 (update_document_status)
            if page_ink(gray) < self.settings.blank_max_ink:
                page["status"] = "blank"
            return self._close_page(page)
        if not tpl.has_cells:
            page["status"] = "classified_only"
            return self._close_page(page)

        # align — 동시 판의 묶음이면 판마다 정합해 고른다. 판이 하나뿐인 양식은 지금처럼 한 번만.
        # 쪽이 돌아 있으면(첫 정합의 호모그래피로 읽는다) 정확히 세워 다시 정합한다 — 동시 판이면 세운 쪽으로 판을 다시 고른다 (tasks/0007 4.4).
        # 저장하는 호모그래피는 렌더링한 원래 쪽 → 템플릿 (세우는 회전을 합성한 것), 정합 그림은 세운 쪽의 것
        if group:
            (ar, tpl, errs), rotation, _upright = align_upright(gray, lambda g: _variant_first(self._align_variants(g, group)))
            page["template_name"], page["variant_errs"] = tpl.name, json.dumps(errs, sort_keys=True)
        else:
            ar, rotation, _upright = align_upright(
                gray, lambda g: align_to_template(g, tpl.reference, tpl.regions, ref_features=tpl.features))
        page["rotation"] = rotation
        page.update(align_inliers=ar.n_inliers, align_grid_err=_finite(ar.grid_err_px), align_ok=int(ar.ok),
                    homography=homography_json(ar.homography) if ar.n_inliers else None,
                    render_dpi=self.settings.dpi)
        if self.settings.save_aligned and ar.n_inliers:
            rel = Path("aligned") / document_id / f"p{page_no:02d}_{tpl.name}.png"
            imwrite(self.settings.work_root / rel, ar.warped)
            page["aligned_image"] = rel.as_posix()
        if not ar.ok:
            page["status"] = "align_failed"
            return self._close_page(page)
        if not day:                                 # 날짜가 없는 쪽은 핸들러에 넘기지 않는다 — 업무 행에 날짜 없는 행이 생기지 않게
            raise ValueError("날짜가 없는 쪽입니다 (문서의 날짜를 정한 뒤 다시 처리합니다)")   # (4.1, process_document 를 거치면 오지 않는다)

        # 다시 스캔한 쪽 (4.6): 같은 날짜·계열의 앞 순서 적재된 쪽과 손글씨 자리의 서명이 기준 이상 비슷하면 붙잡는다 — 필드·업무 행 없이.
        # keep(다른 종이다)이 있으면 견주지 않는다. 서명은 적재된 쪽만 남긴다 (뒤 쪽의 견줄 거리)
        binary = binarize(ar.warped)
        sig = sigs.signature(ar.warped, tpl.signature_mask, binary=binary)
        family = tpl.sig_family
        if not (decided is not None and decided.page(page_no).keep):
            match = self._earlier_match(page, family, sig)
            if match is not None and match[1] >= self.settings.dup_min_sim:
                page.update(status="duplicate", duplicate_of=match[0], duplicate_sim=round(match[1], 6))
                s["duplicates"].append({"page_id": page_id, "of": match[0], "sim": round(match[1], 4)})
                return self._close_page(page)

        # extract → (recognize → correct → validate → load: 핸들러)
        # 인쇄 층(tasks/0006 4.3): 있으면 role 표의 형식 있는 칸의 잉크를 인쇄를 뺀 이진 그림으로 잰다. 어느 층으로 쟀는지 쪽에 남긴다
        print_mask = tpl.print_mask if tpl.uses_print_layer else None
        page["print_sha"] = tpl.print_sha if print_mask is not None else None
        upsert(self.con, "doc_page", page)      # doc_field 가 참조하므로 먼저 적는다
        handler = self._handler(tpl.handler)
        # 원본 쪽은 인식기가 원본 해상도 규격을 원할 때만, 쪽마다 한 번 렌더링한다 (PageImages)
        is_image = source_path is not None and Path(source_path).suffix.lower() in IMAGE_EXT
        images = PageImages(aligned=ar.warped, source=source_path, page_no=page_no, homography=ar.homography,
                            render_dpi=self.settings.dpi, source_dpi=self.settings.source_dpi,
                            damaged=self.settings.damaged_pdf, source_image=gray if is_image else None)
        obs = observe_cells(ar.warped, tpl, print_mask, binary=binary)
        # 메타 필드를 핸들러보다 먼저 읽는다 — 쪽 메타가 핸들러가 행을 만들기 전에 정해져 있어야 한다 (tasks/0004 단계 5)
        meta_obs, machine, reads = self._read_meta(tpl, obs, images)
        # 쪽 메타: 검수값 > 라벨 > 파일명 > 기계 값 — 출처·대조와 함께 doc_page_meta 에. 핸들러는 그 최종 값을 쓴다
        human = pagemeta.human_values(self.con, self.site, source_name, page_no, page_id, tpl,
                                      decided.page_date(page_no) if decided is not None else None)
        meta_rows = pagemeta.resolve(page_id, tpl, human, machine)
        pagemeta.write(self.con, page_id, meta_rows)
        meta = pagemeta.final_meta(meta_rows)
        ctx = PageContext(self.con, self.settings, self.site, tpl, document_id, page_id, page_no, source_name,
                          meta, ar.warped, [o for o in obs if id(o) not in meta_obs], self.recognizer, self.corrector,
                          images=images, print_mask=print_mask)
        if reads:                                   # 읽은 메타 필드의 doc_field 행 (기계 값 + 검수)
            upsert(self.con, "doc_field", apply_reviews(ctx, [_meta_field_row(ctx, o, r) for o, r in reads]))
        result = handler.load(ctx)
        page["status"] = "loaded"
        upsert(self.con, "doc_page_sig", {"page_id": page_id, "document_id": document_id, "family": family, "work_date": day,
                                          "sig": sigs.encode(sig)})
        hs = s["handlers"].setdefault(tpl.handler, {})
        for k, v in result.items():
            if isinstance(v, int | float):
                hs[k] = hs.get(k, 0) + v
        out = self._close_page(page)
        out.update(result)
        return out

    def _align_variants(self, gray: np.ndarray, group: list[str]) -> tuple:
        """동시 판마다 정합해 하나를 고른다 (tasks/0006 4.6): 통과한 판 중 괘선 오차가 가장 작은 판. 오차의 차이가 0.5 px 이내면
        그 판에 정합한 인라이어가 많은 판, 그래도 같으면 이름 순서. 통과한 판이 없으면 같은 규칙으로 고른 판의 수치로 align_failed.
        0.5 px: 괘선 오차는 재검출한 괘선 자리(정수)와의 거리의 중앙값이라 0.5 px 단위다 — 같거나 한 단위 차이는 가르지 않는다
        (실제 중기운행일보의 판 B 쪽은 두 판의 오차 차이가 4 px 이상이었다 — tasks/0006 1절). 새 판정 임계값이 아니라 단위다.
        돌려주는 값: (고른 판의 템플릿, 그 정합 결과, {판 이름: 괘선 오차 | None(유한하지 않음)})."""
        results = []
        for n in sorted(group):
            t = self.site.templates[n]
            results.append((t, align_to_template(gray, t.reference, t.regions, ref_features=t.features)))
        errs = {t.name: _finite(ar.grid_err_px) for t, ar in results}
        return (*choose_variant(results), errs)

    def _read_meta(self, tpl, obs, images: PageImages) -> tuple[set, dict, list]:
        """모델이 있는 메타 필드를 읽는다. 돌려주는 값: (읽은 칸의 id — 핸들러에 넘기지 않는다, {키: MachineRead},
        [(칸, (Choice | None, 상태, 모델))]). 잉크가 전혀 없을 때만 읽지 않고 empty.
        잉크 비율로 거르지 않는다: 큰 필드에 쓴 "1" 하나는 세로 획이라 괘선 지우기(cells.remove_rules)가 지워 잉크가 0.0004 까지
        내려갔다 (합성 월 필드, 다른 필드는 0.03 이상) — 칸의 잉크 문턱(0.006–0.008)이면 쓴 날짜를 빈 칸으로 잘못 친다.
        빈 필드는 모델이 거절하고(검수 대기) 끝난다."""
        if not self.meta_readers:
            return set(), {}, []
        used, machine, reads = set(), {}, []
        for o in obs:
            key = o.cell.col_meta.get("meta_key") if o.cell.region == "fields" else None
            reader = self.meta_readers.get(key) if key else None
            if reader is None:
                continue
            used.add(id(o))
            if o.ink <= 0.0:
                machine[key] = pagemeta.MachineRead(None, None, "empty")
                reads.append((o, (None, "empty", reader)))
                continue
            if key not in self._meta_candidates:
                self._meta_candidates[key] = reader.candidates(key, self.site)
            c = reader.read(crop_cell(images, o.cell.bbox, reader.spec), key, self._meta_candidates[key])
            status = reader.status(c)
            machine[key] = pagemeta.MachineRead(c.value, float(c.confidence), status)
            reads.append((o, (c, status, reader)))
        return used, machine, reads

    def _close_page(self, page: dict) -> dict:
        upsert(self.con, "doc_page", page)
        s = self.summary
        s["pages"] += 1
        s["by_status"][page["status"]] = s["by_status"].get(page["status"], 0) + 1
        form = page["template_name"] or "unknown"
        s["by_form"][form] = s["by_form"].get(form, 0) + 1
        return dict(page)

    def _handler(self, name: str):
        if name not in self._handlers:
            self._handlers[name] = get_handler(name)
        return self._handlers[name]

    # ── 마무리 ─────────────────────────────────────────────────────────────
    def finalize(self, dates: set[str] | None = None, equipment: set[str] | None = None, commit: bool = True) -> dict:
        """등록된 핸들러 전부의 마무리 (교차검증·연속성·점검 행). dates·equipment 가 None 이면 전부 — 모든 문서를 처리한 뒤.
        이번 프로세스에서 쪽을 적재하지 않은 핸들러도 부른다 (버리기만 한 처리에서도 그 날짜가 다시 계산되게 — 4.8).
        다시 계산한 범위를 touched 에 남긴다 (범위 없이 부르면 전부 — tasks/0008 4.7)."""
        if dates is None and equipment is None:
            self.touched.everything = True
        else:
            self.touched.dates.update(d for d in (dates or ()) if d)
            self.touched.refs.update(r for r in (equipment or ()) if r)
        for name in REGISTRY:
            extra = self._handler(name).finalize(self.con, self.site, self.settings, dates=dates, equipment=equipment)
            if extra and any(extra.values()):
                self.summary.setdefault("finalize", {})[name] = extra
        if commit and self.con.in_transaction:
            self.con.commit()
        return self.summary


class Footprint:
    """문서 하나가 DB 에 있는 범위: 쪽의 날짜, 장비(equipment_ref), 쪽 ID, 다시 스캔을 견주는 묶음(날짜, 계열). 다시 계산할 범위를 정한다."""

    def __init__(self, dates: set[str], refs: set[str], pages: set[str], groups: set[tuple[str, str]] = frozenset()):
        self.dates, self.refs, self.pages, self.groups = set(dates), set(refs), set(pages), set(groups)

    def __or__(self, other: Footprint) -> Footprint:
        return Footprint(self.dates | other.dates, self.refs | other.refs, self.pages | other.pages, self.groups | other.groups)


def _received_now(source_rel: str | None) -> str:
    """받은 시각: 접수한 문서(보관 폴더 intake/ 아래)는 폴더 이름의 시각 — DB 를 지우고 보관 폴더를 다시 돌려도 같다. 아니면 지금."""
    from ..intake.inbox import received_from_rel

    return received_from_rel(source_rel) or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _page_row(page_id: str, document_id: str, page_no: int, work_date: str | None) -> dict:
    return {"page_id": page_id, "document_id": document_id, "page_no": page_no, "template_name": None,
            "classify_margin": None, "align_inliers": None, "align_grid_err": None, "align_ok": None,
            "aligned_image": None, "homography": None, "render_dpi": None, "print_sha": None, "variant_errs": None,
            "rotation": None, "duplicate_of": None, "duplicate_sim": None,
            "work_date": work_date, "status": "unknown_form", "error": None}


def _meta_field_row(ctx: PageContext, o, read) -> dict:
    """읽은 메타 필드 하나의 doc_field 행. 기계 값은 value_raw·confidence·candidates·backend·status_raw 에 — 검수가 건드리지 않는다.
    backend = "meta-<읽는 법>" (meta-digits | meta-choice). 자동 적재가 아니면 검수 대기 (목록에 없는 값·거절 포함)."""
    c, status, reader = read
    if c is None:                                              # 잉크 없음: 빈 필드로 확정
        return field_row(ctx, o, has_value=False, value_raw="", value_final="", confidence=1.0, candidates=None,
                         backend="ink", review_status="auto")
    return field_row(ctx, o, has_value=True, value_raw=c.value, value_final=c.value, confidence=float(c.confidence),
                     candidates=c.candidates, backend=f"meta-{reader.reader}",
                     review_status="auto" if status == "auto" else "pending")


def _variant_first(chosen: tuple) -> tuple:
    """_align_variants 의 (템플릿, AlignResult, 오차) → (AlignResult, 템플릿, 오차) — align_upright 는 첫 값을 정합 결과로 읽는다."""
    tpl, ar, errs = chosen
    return ar, tpl, errs


VARIANT_TIE_PX = 0.5        # 동시 판의 괘선 오차가 이만큼 안이면 인라이어로 가른다 — 괘선 오차의 단위 (Pipeline._align_variants)


def choose_variant(results: list[tuple]) -> tuple:
    """[(템플릿, AlignResult)] (이름순) → 고른 (템플릿, AlignResult). Pipeline._align_variants 의 규칙: 통과한 것(없으면 전부) 중
    괘선 오차가 가장 작은 것에서 VARIANT_TIE_PX 안의 것들 → 인라이어가 많은 것 → 이름이 앞인 것."""
    pool = [r for r in results if r[1].ok] or list(results)
    low = min(ar.grid_err_px for _t, ar in pool)
    near = [r for r in pool if r[1].grid_err_px <= low + VARIANT_TIE_PX]
    return min(near, key=lambda r: (-r[1].n_inliers, r[0].name))


BAD_PAGE_STATUSES = ("unknown_form", "align_failed", "error", "duplicate")   # 사람이 봐야 하는 쪽 (blank·discarded 는 아니다)


def document_status(con: sqlite3.Connection, document_id: str) -> str:
    """처리한 문서의 상태: 검수 대기 필드가 있거나 양식·정합에 실패한 쪽, 쪽 오류, 다시 스캔 의심 쪽이 있으면 needs_review."""
    n_pending = con.execute(
        "SELECT COUNT(*) FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
        "WHERE p.document_id = ? AND f.review_status = 'pending'", (document_id,)).fetchone()[0]
    n_bad = con.execute(f"SELECT COUNT(*) FROM doc_page WHERE document_id = ? AND status IN "
                        f"({','.join('?' * len(BAD_PAGE_STATUSES))})", (document_id, *BAD_PAGE_STATUSES)).fetchone()[0]
    return "needs_review" if (n_pending or n_bad) else "processed"


def update_document_status(con: sqlite3.Connection, document_id: str) -> str | None:
    """검수를 저장한 직후: processed 와 needs_review 사이에서만 바꾼다 — received·needs_date·discarded·failed 는 그대로 (4.1).
    돌려주는 값: 그 뒤의 문서 상태."""
    con.execute("UPDATE doc_document SET status = ? WHERE document_id = ? AND status IN ('processed', 'needs_review')",
                (document_status(con, document_id), document_id))
    row = con.execute("SELECT status FROM doc_document WHERE document_id = ?", (document_id,)).fetchone()
    return row[0] if row is not None else None


def homography_json(h) -> str:
    """3×3 호모그래피 → JSON. 반올림하지 않는다: 원근 항(셋째 행)은 1e-7 크기라 소수 6자리로 자르면 0 이 되어
    쪽의 구석에서 원본 해상도 크롭이 몇 픽셀 어긋난다 (실데이터에서 최대 3.6 px)."""
    return json.dumps(np.asarray(h, dtype=float).tolist())


def con_count(con: sqlite3.Connection, sql: str, *args) -> int:
    return con.execute(sql, args).fetchone()[0]


def _error_text(e: BaseException) -> str:
    """예외 종류와 메시지만. 셀 값은 적지 않는다."""
    return f"{type(e).__name__}: {e}"[:500]


def _finite(v: float) -> float | None:
    return None if v == float("inf") else float(v)
