"""파이프라인 실행기.

  ingest    파일 해시로 문서 등록(중복 차단), 페이지 분리
  classify  페이지가 어느 양식인지
  align     양식 기준 이미지로 정합, 품질 점수
  extract   셀 크롭·잉크 관측
  recognize → correct → validate → load   — 양식의 핸들러가 수행 (handlers/)
  finalize  모든 문서를 처리한 뒤 양식 간 교차검증

이 파일은 단계 순서와 상태 기록만 안다. 양식의 의미는 핸들러가, 글자 읽기는 인식 백엔드가 안다.
같은 파일을 다시 넣으면 같은 키로 덮어쓰므로(멱등) 인식기를 바꾼 뒤 그대로 다시 돌리면 된다.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..config import Settings
from ..correct import Corrector, get_corrector
from ..forms.classify import FormClassifier
from ..forms.sitepack import SitePack
from ..handlers import PageContext, get_handler
from ..imaging.align import align_to_template
from ..imaging.cells import observe_cells
from ..imaging.io import SUPPORTED_EXT, imwrite, load_pages
from ..recognize import Recognizer, get_recognizer
from ..store.db import open_db, upsert


class Pipeline:
    def __init__(self, settings: Settings, site: SitePack | None = None, recognizer: Recognizer | None = None,
                 corrector: Corrector | None = None, con: sqlite3.Connection | None = None):
        self.settings = settings
        if site is None:
            if settings.site is None:
                raise ValueError("사이트 팩 경로가 없습니다. 설정의 [paths] site 또는 MINEDOCSCAN_SITE 를 지정하세요.")
            site = SitePack(settings.site)
        self.site = site
        self.recognizer = recognizer or get_recognizer(settings.recognizer)
        self.corrector = corrector or get_corrector(settings.corrector)
        self.con = con or open_db(settings.resolved_db_url)
        self.classifier = FormClassifier(list(site.templates.values()))
        self._handlers: dict[str, object] = {}
        self.summary: dict = {"documents": 0, "pages": 0, "by_form": {}, "by_status": {}, "low_margin": [],
                              "handlers": {}}

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

    def run(self, paths: list[str | Path], template: str | None = None) -> dict:
        for f in self.expand(paths):
            self.process_file(f, template=template)
        return self.finalize()

    # ── 문서 하나 ──────────────────────────────────────────────────────────
    def process_file(self, path: str | Path, template: str | None = None) -> dict:
        path = Path(path)
        document_id = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        source_name = path.stem
        doc_meta = self.site.page_meta(source_name, 0)
        upsert(self.con, "doc_document", {
            "document_id": document_id, "source_path": str(path), "source_name": source_name,
            "work_date": doc_meta.get("date"), "n_pages": None, "status": "received",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })
        pages = []
        for page_no, gray in load_pages(path, self.settings.dpi):
            pages.append(self.process_page(document_id, source_name, page_no, gray, template))
        n_pending = self.con.execute(
            "SELECT COUNT(*) FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
            "WHERE p.document_id = ? AND f.review_status = 'pending'", (document_id,)).fetchone()[0]
        needs_review = n_pending > 0 or any(p["status"] in ("unknown_form", "align_failed") for p in pages)
        self.con.execute("UPDATE doc_document SET n_pages=?, status=? WHERE document_id=?",
                         (len(pages), "needs_review" if needs_review else "processed", document_id))
        self.con.commit()
        self.summary["documents"] += 1
        return {"document_id": document_id, "pages": pages}

    # ── 페이지 하나 ────────────────────────────────────────────────────────
    def process_page(self, document_id: str, source_name: str, page_no: int, gray: np.ndarray,
                     template: str | None = None) -> dict:
        s = self.summary
        page_id = f"{document_id}-p{page_no}"
        meta = self.site.page_meta(source_name, page_no)
        page = {"page_id": page_id, "document_id": document_id, "page_no": page_no, "template_name": None,
                "classify_margin": None, "align_inliers": None, "align_grid_err": None, "align_ok": None,
                "aligned_image": None, "work_date": meta.get("date"), "status": "unknown_form"}
        s["pages"] += 1

        # classify
        if template:
            name, margin = template, None
        else:
            cr = self.classifier.classify(gray)
            name, margin = cr.template, cr.margin
            if name and margin < self.settings.classify_min_margin:
                s["low_margin"].append({"page_id": page_id, "template": name, "margin": round(margin, 2)})
        page["template_name"], page["classify_margin"] = name, margin
        s["by_form"][name or "unknown"] = s["by_form"].get(name or "unknown", 0) + 1
        tpl = self.site.templates.get(name) if name else None
        if tpl is None:
            return self._close_page(page)
        if not tpl.has_cells:
            page["status"] = "classified_only"
            return self._close_page(page)

        # align
        ar = align_to_template(gray, tpl.reference, tpl.regions, ref_features=tpl.features)
        page.update(align_inliers=ar.n_inliers, align_grid_err=_finite(ar.grid_err_px), align_ok=int(ar.ok))
        if self.settings.save_aligned and ar.n_inliers:
            rel = Path("aligned") / document_id / f"p{page_no:02d}_{tpl.name}.png"
            imwrite(self.settings.work_root / rel, ar.warped)
            page["aligned_image"] = rel.as_posix()
        if not ar.ok:
            page["status"] = "align_failed"
            return self._close_page(page)

        # extract → (recognize → correct → validate → load: 핸들러)
        upsert(self.con, "doc_page", page)      # doc_field 가 참조하므로 먼저 적는다
        handler = self._handler(tpl.handler)
        ctx = PageContext(self.con, self.settings, self.site, tpl, document_id, page_id, page_no, source_name,
                          meta, ar.warped, observe_cells(ar.warped, tpl), self.recognizer, self.corrector)
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
        self.summary["by_status"][page["status"]] = self.summary["by_status"].get(page["status"], 0) + 1
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


def _finite(v: float) -> float | None:
    return None if v == float("inf") else float(v)
