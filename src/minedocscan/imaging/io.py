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


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def image_size(path: str | Path) -> tuple[int, int]:
    """그림의 (너비, 높이). PNG 는 머리(IHDR)만 읽는다 — 사이트 팩을 읽을 때마다 큰 그림을 풀지 않게. 그 밖의 형식은 풀어서 잰다."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) == 24 and head[:8] == PNG_SIGNATURE and head[12:16] == b"IHDR":
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    h, w = imread_gray(path).shape[:2]
    return w, h


def imwrite(path: str | Path, img: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(path.suffix or ".png", img)
    if not ok:
        raise ValueError(f"이미지를 인코딩할 수 없습니다: {path}")
    buf.tofile(str(path))


class DamagedPdfError(ValueError):
    pass


DAMAGED_PDF_POLICIES = ("fail", "warn")


def _open_pdf(path: Path, damaged: str = "fail", warnings: list[str] | None = None):
    """PDF 를 연다. 라이브러리의 오류 메시지는 표준 출력에 찍지 않는다(--json).

    라이브러리가 조용히 복구해서 연 파일(동기화 중 잘린 파일이 가장 흔하다)의 처리는 damaged 가 정한다:
      fail  오류로 낸다 (기본) — 뒤쪽 쪽이 사라진 채 '양식 없음'으로 섞이면 손상을 알 수 없다
      warn  그대로 연다. warnings 에 한 줄 남긴다 — 스캐너가 만든 멀쩡한 파일이 '복구 필요'로 읽힐 때를 위한 것
    열 수조차 없는 파일(쓰레기 바이트, 0바이트)과 쪽이 하나도 없는 파일은 어느 쪽이든 오류다.
    """
    import pymupdf

    if damaged not in DAMAGED_PDF_POLICIES:
        raise ValueError(f"damaged_pdf 는 {DAMAGED_PDF_POLICIES} 중 하나: {damaged!r}")
    pymupdf.TOOLS.mupdf_display_errors(False)
    doc = pymupdf.open(str(path))
    if doc.page_count == 0:
        doc.close()
        raise DamagedPdfError(f"PDF 에 쪽이 없습니다: {path.name}")
    if doc.is_repaired:
        if damaged != "warn":
            doc.close()
            raise DamagedPdfError(f"PDF 가 손상되어 복구가 필요했습니다 (잘린 파일?): {path.name}. 원본을 다시 받으세요 "
                                  "(스캐너가 만든 멀쩡한 파일이면 [pipeline] damaged_pdf = \"warn\")")
        if warnings is not None:
            warnings.append(f"PDF 가 손상되어 복구해서 열었습니다 (쪽 {doc.page_count}개): {path.name}")
    return doc


def _render_page(page, dpi: int) -> np.ndarray:
    import pymupdf

    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy()


def load_pages(path: str | Path, dpi: int = 200, damaged: str = "fail",
               warnings: list[str] | None = None) -> Iterator[tuple[int, np.ndarray]]:
    """파일 하나를 (페이지 번호, 회색조 이미지) 로 푼다. PDF 는 지정 dpi 로 렌더링한다.

    이미지 파일은 스캔 해상도 그대로 쓴다 — 정합 단계의 호모그래피가 배율 차이를 흡수한다.
    damaged·warnings: 손상 PDF 의 처리 (_open_pdf).
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        with _open_pdf(path, damaged, warnings) as doc:
            for i, page in enumerate(doc, 1):
                yield i, _render_page(page, dpi)
    elif ext in IMAGE_EXT:
        yield 1, imread_gray(path)
    else:
        raise ValueError(f"지원하지 않는 형식입니다: {path}")


def load_page(path: str | Path, page_no: int, dpi: int = 200, damaged: str = "fail") -> np.ndarray:
    """한 쪽만 렌더링한다 (1부터). 원본 해상도 크롭처럼 쪽 하나가 필요할 때 — 앞쪽을 전부 렌더링하지 않는다."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        with _open_pdf(path, damaged) as doc:
            if not 1 <= page_no <= doc.page_count:
                raise KeyError(f"{path} 에 {page_no}쪽이 없습니다 (전체 {doc.page_count}쪽)")
            return _render_page(doc[page_no - 1], dpi)
    if page_no != 1:
        raise KeyError(f"{path} 는 이미지 한 장입니다 ({page_no}쪽 없음)")
    return imread_gray(path)


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
