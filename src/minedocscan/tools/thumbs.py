"""쪽 미리보기: 원본 쪽을 1/4 로 줄인 PNG. 양식을 못 찾은 쪽이 무엇인지 눈으로 보기 위해서다.

WORK_ROOT/thumbs/<상태>/<파일명>#<쪽>-<문서 ID>.png 에 쓴다 (저장소 밖). 실제 문서가 보이므로 문서·PR·이슈에 붙이지 않는다.
파일명은 문서마다 하나가 아니다 (스캐너는 같은 이름을 다시 쓴다 — tasks/0007 4.7) — 그래서 문서 ID 를 붙인다.
방향을 아는 쪽(doc_page.rotation)은 세워서 쓴다 (tasks/0007 4.4).
원본은 doc_document.source_path 로, 없으면 archive_root + source_rel 로 찾는다.
"""
from __future__ import annotations

from pathlib import Path

import cv2

from ..imaging.align import rotate_upright
from ..imaging.io import imwrite, load_pages, resolve_source


def source_file(settings, row: dict) -> Path | None:
    """쪽(또는 문서) 행의 원본 파일. 이 컴퓨터에서 닿는 경로를 돌려주고, 없으면 None."""
    return resolve_source(row.get("source_path"), row.get("source_rel"), settings.archive_root)


def write_thumbs(settings, rows: list[dict], out_dir: str | Path | None = None, factor: int = 4) -> list[Path]:
    """rows: report.list_pages() 의 행. 같은 문서의 쪽은 한 번만 연다. 돌려주는 값: 쓴 파일 목록."""
    out = Path(out_dir) if out_dir else Path(settings.work_root) / "thumbs"
    by_doc: dict[str, list[dict]] = {}
    for r in rows:
        by_doc.setdefault(r["document_id"], []).append(r)
    written: list[Path] = []
    for _doc, pages in by_doc.items():
        src = source_file(settings, pages[0])
        if src is None:
            continue
        wanted = {r["page_no"]: r for r in pages}
        for page_no, gray in load_pages(src, max(1, settings.dpi // factor), settings.damaged_pdf):   # PDF 는 1/4 해상도로
            if page_no not in wanted:
                continue
            if src.suffix.lower() != ".pdf":                                   # 이미지는 그대로 들어오므로 줄인다
                gray = cv2.resize(gray, None, fx=1 / factor, fy=1 / factor, interpolation=cv2.INTER_AREA)
            r = wanted[page_no]
            if r.get("rotation"):
                gray = rotate_upright(gray, int(r["rotation"]))
            path = out / r["status"] / f"{r['source']}-{r['document_id']}.png"
            imwrite(path, gray)
            written.append(path)
    return written
