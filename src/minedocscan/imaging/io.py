"""이미지·PDF 입출력.

cv2.imread / cv2.imwrite 는 Windows 에서 한글 경로를 읽고 쓰지 못한다. 현장 파일명은 대부분 한글이므로
항상 여기의 함수를 쓴다 (np.fromfile + imdecode, imencode + tofile).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
SUPPORTED_EXT = IMAGE_EXT | {".pdf"}


def imread_gray(path: str | Path) -> np.ndarray:
    buf = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"이미지를 읽을 수 없습니다: {path}")
    return img


def imwrite(path: str | Path, img: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise ValueError(f"이미지를 인코딩할 수 없습니다: {path}")
    buf.tofile(str(path))


def load_pages(path: str | Path, dpi: int = 200) -> Iterator[tuple[int, np.ndarray]]:
    """파일 하나를 (페이지 번호, 회색조 이미지) 로 푼다. PDF 는 지정 dpi 로 렌더링한다.

    이미지 파일은 스캔 해상도 그대로 쓴다 — 정합 단계의 호모그래피가 배율 차이를 흡수한다.
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        import pymupdf

        with pymupdf.open(str(path)) as doc:
            for i, page in enumerate(doc, 1):
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
                yield i, np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy()
    elif ext in IMAGE_EXT:
        yield 1, imread_gray(path)
    else:
        raise ValueError(f"지원하지 않는 형식입니다: {path}")


def resolve_source(source_path: str | None, source_rel: str | None, archive_root: str | Path | None) -> Path | None:
    """문서의 원본 파일 중 이 컴퓨터에서 닿는 경로. 절대경로가 없으면 archive_root + 상대경로. 둘 다 없으면 None."""
    if source_path:
        p = Path(source_path)
        if p.exists():
            return p
    if source_rel and archive_root is not None:
        q = Path(archive_root) / source_rel
        if q.exists():
            return q
    return None
