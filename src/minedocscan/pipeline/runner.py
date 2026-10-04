"""파이프라인 실행기.

  ingest    파일 해시로 문서 등록(중복 차단), 페이지 분리
  classify  페이지가 어느 양식인지
  align     양식 기준 이미지로 정합, 품질 점수
  extract   셀 크롭·잉크 관측
  recognize → correct → validate → load   — 양식의 핸들러가 수행 (handlers/)
  finalize  모든 문서를 처리한 뒤 양식 간 교차검증

이 파일은 단계 순서와 상태 기록만 안다. 양식의 의미는 핸들러가, 글자 읽기는 인식 백엔드가 안다.
같은 파일을 다시 넣으면 같은 키로 덮어쓰므로(멱등) 인식기를 바꾼 뒤 그대로 다시 돌리면 된다.
시작할 때 사이트 팩의 검수 파일을 doc_review 로 읽어 들이므로, DB 를 지우고 다시 돌려도 사람이 입력한 값은 다시 붙는다.

전체 묶음(수백 파일)을 돌릴 때 (tasks/0002 4.2, 4.3):
  · 한 문서의 실패가 전체를 멈추지 않는다. 문서를 읽지 못하면 그 문서는 failed(롤백), 쪽 하나에서 예외가 나면 그 쪽만 error(SAVEPOINT).
    strict=True 면 첫 오류에서 멈춘다(디버깅용). 실패는 summary["failed"] 에 모인다.
  · skip_existing=True 면 같은 파일(문서 해시)이 이미 끝까지 처리된 경우에만 건너뛴다. failed 는 다시 한다.
    템플릿이나 인식기를 바꾼 뒤에는 건너뛰면 안 된다 — 그때는 처음부터 다시 돌린다.
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
from ..handlers import PageContext, get_handler
from ..imaging.align import align_to_template
from ..imaging.cells import observe_cells
from ..imaging.cropspec import PageImages
from ..imaging.io import IMAGE_EXT, SUPPORTED_EXT, imwrite, load_pages
from ..recognize import Recognizer, build_recognizer
from ..review.store import import_into
from ..store.db import open_db, upsert


class Pipeline:
    def __init__(self, settings: Settings, site: SitePack | None = None, recognizer: Recognizer | None = None,
                 corrector: Corrector | None = None, con: sqlite3.Connection | None = None, load_reviews: bool = True):
        """load_reviews=False 면 검수 파일을 읽어 들이지 않는다 — 회귀 검사처럼 기계 값만 봐야 할 때."""
        self.settings = settings
        if site is None:
            if settings.site is None:
                raise ValueError("사이트 팩 경로가 없습니다. 설정의 [paths] site 또는 MINEDOCSCAN_SITE 를 지정하세요.")
            site = SitePack(settings.site)
        self.site = site
        self.recognizer = recognizer or build_recognizer(settings, site)
        self.corrector = corrector or get_corrector(settings.corrector)
        self.con = con or open_db(settings.resolved_db_url)
        self.classifier = FormClassifier(list(site.templates.values()))
        self._handlers: dict[str, object] = {}
        self.summary: dict = {"documents": 0, "pages": 0, "by_form": {}, "by_status": {}, "low_margin": [],
                              "handlers": {}, "skipped": 0, "failed": [], "page_errors": [], "warnings": []}
        self.summary["reviews"] = (import_into(self.con, settings.reviews_path(self.site.root)) if load_reviews
                                   else {"path": None, "imported": 0, "skipped": 0})

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
        for f in self.expand(paths):
            self.process_file(f, template=template, skip_existing=skip_existing, strict=strict)
        return self.finalize()

    # ── 문서 하나 ──────────────────────────────────────────────────────────
    def process_file(self, path: str | Path, template: str | None = None, skip_existing: bool = False,
                     strict: bool = False) -> dict:
        path = Path(path)
        source_name = path.stem
        try:
            document_id = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError as e:
            if strict:
                raise
            document_id = "unreadable-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:5]   # 읽지도 못하면 경로로
            return self._fail_document(document_id, path, source_name, e)
        if skip_existing:
            row = self.con.execute("SELECT status FROM doc_document WHERE document_id = ?", (document_id,)).fetchone()
            n_err = con_count(self.con, "SELECT COUNT(*) FROM doc_page WHERE document_id = ? AND status = 'error'", document_id)
            if row is not None and row[0] in ("processed", "needs_review") and n_err == 0:   # 쪽 오류가 있으면 다시 한다
                self.summary["skipped"] += 1
                return {"document_id": document_id, "status": row[0], "skipped": True, "pages": []}
        snapshot = copy.deepcopy(self.summary)            # 문서가 실패하면 그 문서의 집계도 되돌린다
        warnings: list[str] = []
        try:
            upsert(self.con, "doc_document", self._document_row(document_id, path, source_name, "received", None))
            pages = []
            for page_no, gray in load_pages(path, self.settings.dpi, self.settings.damaged_pdf, warnings):
                pages.append(self.process_page(document_id, source_name, page_no, gray, template, strict=strict,
                                               source_path=path))
            warning = "; ".join(warnings) or None
            self.con.execute("UPDATE doc_document SET n_pages=?, warning=? WHERE document_id=?",
                             (len(pages), warning, document_id))
            update_document_status(self.con, document_id)
            self.con.commit()
        except Exception as e:                            # noqa: BLE001 — 한 문서의 실패가 전체를 멈추지 않는다
            if strict:
                raise
            self.con.rollback()                           # 반쯤 쓰인 행은 남기지 않는다
            self.summary = snapshot
            return self._fail_document(document_id, path, source_name, e)
        # 같은 경로의 옛 failed 기록(깨졌던 바이트의 해시)은 이 성공이 대체한다. 그 바이트는 더 이상 없다
        self.con.execute("DELETE FROM doc_document WHERE source_path = ? AND status = 'failed' AND document_id <> ?",
                         (str(path), document_id))
        self.con.commit()
        self.summary["documents"] += 1
        if warning:                                       # 경고만으로는 종료 코드가 1 이 되지 않는다
            self.summary["warnings"].append({"document_id": document_id, "source_name": source_name, "warning": warning})
        return {"document_id": document_id, "status": "ok", "pages": pages, "warning": warning}

    def _document_row(self, document_id: str, path: Path, source_name: str, status: str, error: str | None) -> dict:
        rel = None
        if self.settings.archive_root is not None:
            try:
                rel = path.resolve().relative_to(Path(self.settings.archive_root).resolve()).as_posix()
            except ValueError:
                rel = None
        return {"document_id": document_id, "source_path": str(path), "source_rel": rel, "source_name": source_name,
                "work_date": self.site.page_meta(source_name, 0).get("date"), "n_pages": None, "status": status,
                "error": error, "warning": None, "created_at": datetime.now(UTC).isoformat(timespec="seconds")}

    def _fail_document(self, document_id: str, path: Path, source_name: str, e: BaseException) -> dict:
        err = _error_text(e)
        upsert(self.con, "doc_document", self._document_row(document_id, path, source_name, "failed", err))
        self.con.commit()
        self.summary["failed"].append({"document_id": document_id, "source_name": source_name, "error": err})
        return {"document_id": document_id, "status": "failed", "error": err, "pages": []}

    # ── 페이지 하나 ────────────────────────────────────────────────────────
    def process_page(self, document_id: str, source_name: str, page_no: int, gray: np.ndarray,
                     template: str | None = None, strict: bool = False, source_path: Path | None = None) -> dict:
        """쪽 하나. 예외가 나면 그 쪽의 행만 되돌리고(SAVEPOINT) status=error 로 남긴다. strict 면 그대로 올린다."""
        if not self.con.in_transaction:
            self.con.execute("BEGIN")                     # SAVEPOINT 가 바깥 트랜잭션 안에 있어야 RELEASE 가 커밋이 되지 않는다
        self.con.execute("SAVEPOINT page")
        snapshot = copy.deepcopy(self.summary)
        page_id = f"{document_id}-p{page_no}"
        meta = self.site.page_meta(source_name, page_no)
        page = {"page_id": page_id, "document_id": document_id, "page_no": page_no, "template_name": None,
                "classify_margin": None, "align_inliers": None, "align_grid_err": None, "align_ok": None,
                "aligned_image": None, "homography": None, "render_dpi": None,
                "work_date": meta.get("date"), "status": "unknown_form", "error": None}
        try:
            out = self._process_page(page, source_name, gray, template, source_path)
            self.con.execute("RELEASE SAVEPOINT page")
            return out
        except Exception as e:                            # noqa: BLE001
            if strict:
                raise
            self.con.execute("ROLLBACK TO SAVEPOINT page")
            self.con.execute("RELEASE SAVEPOINT page")
            self.summary = snapshot
            err = _error_text(e)
            self.summary["page_errors"].append({"page_id": page_id, "error": err})
            page.update(status="error", error=err)        # 어디까지 갔는지(양식·정합)는 남긴다
            return self._close_page(page)

    def _process_page(self, page: dict, source_name: str, gray: np.ndarray, template: str | None = None,
                      source_path: Path | None = None) -> dict:
        s = self.summary
        page_id, document_id, page_no = page["page_id"], page["document_id"], page["page_no"]
        meta = self.site.page_meta(source_name, page_no)

        # classify
        if template:
            name, margin = template, None
        else:
            cands = [t.name for t in self.site.templates_for(meta.get("date"))]     # 그날 유효한 판만 (tasks/0002 4.4)
            cr = self.classifier.classify(gray, candidates=cands)
            name, margin = cr.template, cr.margin
            if name and margin < self.settings.classify_min_margin:
                s["low_margin"].append({"page_id": page_id, "template": name, "margin": round(margin, 2)})
        page["template_name"], page["classify_margin"] = name, margin
        tpl = self.site.templates.get(name) if name else None
        if tpl is None:
            return self._close_page(page)
        if not tpl.has_cells:
            page["status"] = "classified_only"
            return self._close_page(page)

        # align
        ar = align_to_template(gray, tpl.reference, tpl.regions, ref_features=tpl.features)
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

        # extract → (recognize → correct → validate → load: 핸들러)
        upsert(self.con, "doc_page", page)      # doc_field 가 참조하므로 먼저 적는다
        # 쪽 메타: 검수값 > 라벨 > 파일명 (> 기계 값) — 출처와 함께 doc_page_meta 에. 핸들러는 그 최종 값을 쓴다
        human = pagemeta.human_values(self.con, self.site, source_name, page_no, page_id, tpl)
        meta_rows = pagemeta.resolve(page_id, tpl, human)
        pagemeta.write(self.con, page_id, meta_rows)
        meta = pagemeta.final_meta(meta_rows)
        handler = self._handler(tpl.handler)
        # 원본 쪽은 인식기가 원본 해상도 규격을 원할 때만, 쪽마다 한 번 렌더링한다 (PageImages)
        is_image = source_path is not None and Path(source_path).suffix.lower() in IMAGE_EXT
        images = PageImages(aligned=ar.warped, source=source_path, page_no=page_no, homography=ar.homography,
                            render_dpi=self.settings.dpi, source_dpi=self.settings.source_dpi,
                            damaged=self.settings.damaged_pdf, source_image=gray if is_image else None)
        ctx = PageContext(self.con, self.settings, self.site, tpl, document_id, page_id, page_no, source_name,
                          meta, ar.warped, observe_cells(ar.warped, tpl), self.recognizer, self.corrector,
                          images=images)
        result = handler.load(ctx)
        page["status"] = "loaded"
        hs = s["handlers"].setdefault(tpl.handler, {})
        for k, v in result.items():
            if isinstance(v, int | float):
                hs[k] = hs.get(k, 0) + v
        out = self._close_page(page)
        out.update(result)
        return out

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
    def finalize(self) -> dict:
        for name, h in self._handlers.items():
            extra = h.finalize(self.con, self.site, self.settings)
            if extra:
                self.summary.setdefault("finalize", {})[name] = extra
        self.con.commit()
        return self.summary


def update_document_status(con: sqlite3.Connection, document_id: str) -> str:
    """문서 상태를 DB 에서 다시 계산한다: 검수 대기 필드가 있거나 양식·정합에 실패한 쪽이 있으면 needs_review.
    처리 직후와 검수 저장 직후에 같은 규칙을 쓴다."""
    n_pending = con.execute(
        "SELECT COUNT(*) FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
        "WHERE p.document_id = ? AND f.review_status = 'pending'", (document_id,)).fetchone()[0]
    n_bad = con.execute("SELECT COUNT(*) FROM doc_page WHERE document_id = ? AND status IN "
                        "('unknown_form', 'align_failed', 'error')", (document_id,)).fetchone()[0]
    status = "needs_review" if (n_pending or n_bad) else "processed"
    con.execute("UPDATE doc_document SET status=? WHERE document_id=?", (status, document_id))
    return status


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
